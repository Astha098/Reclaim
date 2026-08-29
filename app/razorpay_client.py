"""Razorpay adapter.

One interface, two implementations. `MockRazorpay` mirrors the real API's request
and response shapes field-for-field, so `USE_MOCK_RAZORPAY=false` plus test keys
is the only change needed to go live — no call sites move.

The mock is not a stub that returns `{"ok": true}`. It issues real-looking
`plink_*` ids, serves a working local checkout page, and applies the same
validation the real API does (amount ≥ 100 paise, currency, contact-or-email
required for notification). That means integration bugs surface in mock mode
instead of on the first real key.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import settings

log = logging.getLogger("reclaim.razorpay")

API_BASE = "https://api.razorpay.com/v1"


class RazorpayError(RuntimeError):
    """Non-2xx from the gateway, with the body attached for debugging."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"Razorpay returned {status}: {body}")
        self.status = status
        self.body = body


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """Verify `X-Razorpay-Signature`: HMAC-SHA256 of the raw body, hex digest.

    Two things this gets right that are easy to get wrong:

    * It hashes the **raw bytes**, not a re-serialized parse of the JSON. Any
      round-trip through `json.loads`/`json.dumps` changes whitespace and key
      order and breaks the digest.
    * It compares with `hmac.compare_digest`, not `==`. A plain comparison
      short-circuits on the first differing byte, which leaks the correct prefix
      through timing and lets an attacker forge a signature byte by byte.
    """
    if not signature:
        return False
    secret = settings.razorpay_webhook_secret.encode()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def sign_payload(raw_body: bytes) -> str:
    """Produce a valid signature. Used by the demo seeder to post webhooks that
    pass verification, so the ingest path is exercised exactly as in production
    rather than bypassed for testing."""
    return hmac.new(
        settings.razorpay_webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


@dataclass
class PaymentLink:
    id: str
    short_url: str
    status: str
    amount: int
    reference_id: str
    upi_only: bool = False

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> PaymentLink:
        return cls(
            id=str(body.get("id", "")),
            short_url=str(body.get("short_url", "")),
            status=str(body.get("status", "created")),
            amount=int(body.get("amount", 0)),
            reference_id=str(body.get("reference_id", "")),
            upi_only=bool(body.get("upi_link", False)),
        )


class RazorpayClient(Protocol):
    def create_payment_link(
        self,
        *,
        amount_paise: int,
        reference_id: str,
        description: str,
        contact: str | None,
        email: str | None,
        upi_only: bool = False,
        notes: dict[str, str] | None = None,
    ) -> PaymentLink: ...

    def fetch_payment_link(self, link_id: str) -> dict[str, Any]: ...

    def cancel_payment_link(self, link_id: str) -> dict[str, Any]: ...


def _validate(amount_paise: int, contact: str | None, email: str | None) -> None:
    """Mirror the real API's input validation so mock mode catches the same bugs."""
    if amount_paise < 100:
        raise RazorpayError(400, {"error": {"description": "amount must be at least 100 paise"}})
    if not (contact or email):
        raise RazorpayError(
            400, {"error": {"description": "at least one of contact or email is required"}}
        )


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------


class MockRazorpay:
    """In-memory Razorpay. Response shapes match the live API."""

    def __init__(self) -> None:
        self._links: dict[str, dict[str, Any]] = {}

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        reference_id: str,
        description: str,
        contact: str | None,
        email: str | None,
        upi_only: bool = False,
        notes: dict[str, str] | None = None,
    ) -> PaymentLink:
        _validate(amount_paise, contact, email)
        link_id = f"plink_{secrets.token_hex(7)}"
        body: dict[str, Any] = {
            "id": link_id,
            "entity": "payment_link",
            "status": "created",
            "amount": amount_paise,
            "amount_paid": 0,
            "currency": "INR",
            "accept_partial": False,
            "description": description,
            "reference_id": reference_id,
            "customer": {"contact": contact, "email": email},
            "notify": {"sms": bool(contact), "email": bool(email)},
            "reminder_enable": True,
            "upi_link": upi_only,
            "notes": notes or {},
            # Points at our own simulator so the link in the demo is genuinely
            # clickable and completing it drives the real recovery path.
            "short_url": f"http://localhost:8000/simulate/pay/{link_id}",
        }
        self._links[link_id] = body
        log.info("mock payment link %s for %s (₹%.2f, upi_only=%s)",
                 link_id, reference_id, amount_paise / 100, upi_only)
        return PaymentLink.from_api(body)

    def fetch_payment_link(self, link_id: str) -> dict[str, Any]:
        body = self._links.get(link_id)
        if body is None:
            raise RazorpayError(400, {"error": {"description": "payment link not found"}})
        return body

    def cancel_payment_link(self, link_id: str) -> dict[str, Any]:
        body = self.fetch_payment_link(link_id)
        if body["status"] == "paid":
            raise RazorpayError(400, {"error": {"description": "cannot cancel a paid link"}})
        body["status"] = "cancelled"
        return body

    # -- mock-only helpers -------------------------------------------------

    def mark_paid(self, link_id: str) -> dict[str, Any]:
        body = self.fetch_payment_link(link_id)
        body["status"] = "paid"
        body["amount_paid"] = body["amount"]
        return body

    def all_links(self) -> dict[str, dict[str, Any]]:
        return self._links


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


class LiveRazorpay:
    """Real API over HTTPS with basic auth."""

    def __init__(self, key_id: str, key_secret: str) -> None:
        if not (key_id and key_secret):
            raise RuntimeError(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required when "
                "USE_MOCK_RAZORPAY=false"
            )
        self._client = httpx.Client(
            base_url=API_BASE, auth=(key_id, key_secret), timeout=20.0
        )

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = self._client.request(method, path, **kw)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise RazorpayError(resp.status_code, body)
        return resp.json()

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        reference_id: str,
        description: str,
        contact: str | None,
        email: str | None,
        upi_only: bool = False,
        notes: dict[str, str] | None = None,
    ) -> PaymentLink:
        _validate(amount_paise, contact, email)
        payload: dict[str, Any] = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "reference_id": reference_id,
            "description": description[:2048],
            "customer": {k: v for k, v in (("contact", contact), ("email", email)) if v},
            "notify": {"sms": bool(contact), "email": bool(email)},
            "reminder_enable": True,
            "notes": notes or {},
        }
        # `upi_link` restricts the link to a UPI intent flow — no card form, no
        # 3DS step. Exactly what `auth_abandoned` needs, and the reason that
        # bucket's action is a distinct one rather than a generic link.
        if upi_only:
            payload["upi_link"] = True
        return PaymentLink.from_api(self._request("POST", "/payment_links", json=payload))

    def fetch_payment_link(self, link_id: str) -> dict[str, Any]:
        return self._request("GET", f"/payment_links/{link_id}")

    def cancel_payment_link(self, link_id: str) -> dict[str, Any]:
        return self._request("POST", f"/payment_links/{link_id}/cancel")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: RazorpayClient | None = None


def client() -> RazorpayClient:
    global _client
    if _client is None:
        if settings.use_mock_razorpay:
            log.info("using MockRazorpay (set USE_MOCK_RAZORPAY=false for test keys)")
            _client = MockRazorpay()
        else:
            log.info("using LiveRazorpay against %s", API_BASE)
            _client = LiveRazorpay(settings.razorpay_key_id, settings.razorpay_key_secret)
    return _client


def mock() -> MockRazorpay:
    """Typed accessor for mock-only helpers. Raises if running live."""
    c = client()
    if not isinstance(c, MockRazorpay):
        raise RuntimeError("mock helpers unavailable when USE_MOCK_RAZORPAY=false")
    return c
