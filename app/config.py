from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class CircuitConfig:
    window_minutes: int = field(
        default_factory=lambda: _env_int("HEALTH_WINDOW_MINUTES", 15)
    )
    min_samples: int = field(
        default_factory=lambda: _env_int("HEALTH_MIN_SAMPLES", 20)
    )
    open_below: float = field(
        default_factory=lambda: _env_float("HEALTH_OPEN_BELOW", 0.35)
    )
    close_above: float = field(
        default_factory=lambda: _env_float("HEALTH_CLOSE_ABOVE", 0.60)
    )
    half_open_after_minutes: int = field(
        default_factory=lambda: _env_int(
            "HEALTH_HALF_OPEN_AFTER_MINUTES", 5
        )
    )
    probe_attempts: int = field(
        default_factory=lambda: _env_int("HEALTH_PROBE_ATTEMPTS", 5)
    )


@dataclass
class GuardrailConfig:
    max_attempts_per_order: int = field(
        default_factory=lambda: _env_int(
            "MAX_ATTEMPTS_PER_ORDER", 3
        )
    )
    min_cooldown_minutes: int = field(
        default_factory=lambda: _env_int(
            "MIN_COOLDOWN_MINUTES", 30
        )
    )
    quiet_hours_start: int = field(
        default_factory=lambda: _env_int("QUIET_HOURS_START", 21)
    )
    quiet_hours_end: int = field(
        default_factory=lambda: _env_int("QUIET_HOURS_END", 8)
    )
    max_contacts_per_customer_per_day: int = field(
        default_factory=lambda: _env_int(
            "MAX_CONTACTS_PER_CUSTOMER_PER_DAY", 2
        )
    )
    max_contacts_per_merchant_per_day: int = field(
        default_factory=lambda: _env_int(
            "MAX_CONTACTS_PER_MERCHANT_PER_DAY", 5000
        )
    )


@dataclass
class Settings:
    # Razorpay
    razorpay_key_id: str = field(
        default_factory=lambda: _env("RAZORPAY_KEY_ID", "")
    )
    razorpay_key_secret: str = field(
        default_factory=lambda: _env("RAZORPAY_KEY_SECRET", "")
    )
    razorpay_webhook_secret: str = field(
        default_factory=lambda: _env(
            "RAZORPAY_WEBHOOK_SECRET", "whsec_local_dev"
        )
    )
    use_mock_razorpay: bool = field(
        default_factory=lambda: _env_bool(
            "USE_MOCK_RAZORPAY", True
        )
    )
    verify_webhook_signature: bool = field(
        default_factory=lambda: _env_bool(
            "VERIFY_WEBHOOK_SIGNATURE", True
        )
    )

    # Classifier
    classifier_mode: str = field(
        default_factory=lambda: _env(
            "CLASSIFIER_MODE", "hybrid"
        )
    )

    # Supported providers: anthropic, xai, gemini
    llm_provider: str = field(
        default_factory=lambda: _env(
            "LLM_PROVIDER", "gemini"
        )
    )

    anthropic_api_key: str = field(
        default_factory=lambda: _env(
            "ANTHROPIC_API_KEY", ""
        )
    )

    xai_api_key: str = field(
        default_factory=lambda: _env(
            "XAI_API_KEY", ""
        )
    )

    gemini_api_key: str = field(
        default_factory=lambda: _env(
            "GEMINI_API_KEY", ""
        )
    )

    llm_model: str = field(
        default_factory=lambda: _env(
            "LLM_MODEL", "gemini-3.1-flash-lite"
        )
    )

    llm_abstain_below: float = field(
        default_factory=lambda: _env_float(
            "LLM_ABSTAIN_BELOW", 0.55
        )
    )

    # Runtime
    db_path: Path = field(
        default_factory=lambda: Path(
            _env(
                "DB_PATH",
                str(DATA_DIR / "reclaim.db"),
            )
        )
    )

    timezone: ZoneInfo = field(
        default_factory=lambda: ZoneInfo(
            _env("TIMEZONE", "Asia/Kolkata")
        )
    )

    scheduler_tick_seconds: int = field(
        default_factory=lambda: _env_int(
            "SCHEDULER_TICK_SECONDS", 10
        )
    )

    demo_time_compression: int = field(
        default_factory=lambda: _env_int(
            "DEMO_TIME_COMPRESSION", 1
        )
    )

    dashboard_origin: str = field(
        default_factory=lambda: _env(
            "DASHBOARD_ORIGIN",
            "http://localhost:5173",
        )
    )

    circuit: CircuitConfig = field(
        default_factory=CircuitConfig
    )
    guardrails: GuardrailConfig = field(
        default_factory=GuardrailConfig
    )

    @property
    def llm_available(self) -> bool:
        provider = self.llm_provider.lower().strip()

        if provider == "gemini":
            return bool(self.gemini_api_key)

        if provider == "xai":
            return bool(self.xai_api_key)

        if provider == "anthropic":
            return bool(self.anthropic_api_key)

        return False

    def describe(self) -> dict[str, object]:
        return {
            "razorpay_mode": (
                "mock"
                if self.use_mock_razorpay
                else "test_keys"
            ),
            "classifier_mode": self.classifier_mode,
            "llm_provider": self.llm_provider,
            "llm_available": self.llm_available,
            "llm_model": (
                self.llm_model
                if self.llm_available
                else None
            ),
            "timezone": str(self.timezone),
            "circuit": {
                "window_minutes": self.circuit.window_minutes,
                "min_samples": self.circuit.min_samples,
                "open_below": self.circuit.open_below,
                "close_above": self.circuit.close_above,
                "half_open_after_minutes": (
                    self.circuit.half_open_after_minutes
                ),
                "probe_attempts": self.circuit.probe_attempts,
            },
            "guardrails": {
                "max_attempts_per_order": (
                    self.guardrails.max_attempts_per_order
                ),
                "min_cooldown_minutes": (
                    self.guardrails.min_cooldown_minutes
                ),
                "quiet_hours": [
                    self.guardrails.quiet_hours_start,
                    self.guardrails.quiet_hours_end,
                ],
                "max_contacts_per_customer_per_day": (
                    self.guardrails.max_contacts_per_customer_per_day
                ),
                "max_contacts_per_merchant_per_day": (
                    self.guardrails.max_contacts_per_merchant_per_day
                ),
            },
        }


settings = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)
