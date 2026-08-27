"""Security utilities — webhook signature verification, input sanitization, rate limiting.

Security hardening module for MediAssist AI.
Provides persistent (Supabase-backed) rate limiting, webhook signature
verification, LLM prompt injection detection, and HTTP security headers.
"""

import hmac
import hashlib
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. META WEBHOOK SIGNATURE VERIFICATION (X-Hub-Signature-256)
# ═══════════════════════════════════════════════════════════════════════════


def verify_webhook_signature(
    payload_body: bytes,
    signature_header: Optional[str],
    app_secret: str,
) -> bool:
    """
    Verify Meta X-Hub-Signature-256 header.

    Meta signs every webhook payload with HMAC-SHA256 using your App Secret.
    If this doesn't match, the request was NOT from Meta — reject it.

    Fails CLOSED by default in every environment when app_secret is missing.
    The only way to accept unsigned webhooks is the explicit
    ALLOW_UNSIGNED_WEBHOOKS_DEV=true flag, and only when APP_ENV=development —
    never a silently-missing secret in a misconfigured staging/prod deploy.

    Args:
        payload_body: Raw request body bytes (before JSON parsing).
        signature_header: Value of X-Hub-Signature-256 header.
        app_secret: Your Meta App Secret (from Meta Developer Console).

    Returns:
        True if signature is valid, False otherwise.
    """
    if not app_secret:
        if settings.app_env == "development" and settings.allow_unsigned_webhooks_dev:
            logger.warning(
                "META_APP_SECRET not configured — signature verification SKIPPED "
                "(ALLOW_UNSIGNED_WEBHOOKS_DEV=true, app_env=development only)."
            )
            return True
        logger.error(
            "META_APP_SECRET not configured — REJECTING webhook (fail-closed). "
            "Set META_APP_SECRET, or ALLOW_UNSIGNED_WEBHOOKS_DEV=true for local dev only."
        )
        return False

    if not signature_header:
        logger.warning("Webhook request missing X-Hub-Signature-256 header — REJECTED")
        return False

    # Meta format: "sha256=<hex_digest>"
    if not signature_header.startswith("sha256="):
        logger.warning("Webhook signature has invalid format — REJECTED")
        return False

    expected_signature = (
        "sha256="
        + hmac.new(
            app_secret.encode("utf-8"),
            payload_body,
            hashlib.sha256,
        ).hexdigest()
    )

    is_valid = hmac.compare_digest(expected_signature, signature_header)

    if not is_valid:
        logger.warning(
            "Webhook signature mismatch — REJECTED (possible spoofed request)"
        )

    return is_valid


# ═══════════════════════════════════════════════════════════════════════════
# 2. LLM PROMPT INJECTION SANITIZER
# ═══════════════════════════════════════════════════════════════════════════

# Patterns that indicate prompt injection attempts
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|context)",
    r"you\s+are\s+now\s+(an?\s+)?(admin|administrator|root|superuser|developer|hacker)",
    r"system\s*:\s*",  # Trying to inject system-role messages
    r"act\s+as\s+(if\s+)?(an?\s+)?(admin|root|developer|hacker|unrestricted)",
    r"(show|display|list|reveal|dump|print)\s+(all|every|my)?\s*(patient|appointment|record|data|database|secret|key|token|password)",
    r"forget\s+(all\s+)?(your\s+)?(rules?|instructions?|training|constraints?)",
    r"(bypass|override|disable|skip|break)\s+(security|restrictions?|rules?|filters?|safety)",
    r"pretend\s+(you\s+)?(are|to\s+be)\s+(not\s+)?(a|an)?\s*(ai|bot|assistant|chatbot)",
    r"execute\s+(this\s+)?(sql|query|command|code|script)",
    r"(drop|delete|truncate|alter)\s+table",  # SQL injection in chat
    r"\{[^}]*role\s*:\s*['\"]?system",  # JSON role injection
]

_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def sanitize_user_input(message: str) -> tuple[str, bool]:
    """
    Sanitize user input before passing to LLM.

    Returns:
        (sanitized_message, is_suspicious) — the cleaned message and whether
        prompt injection was detected.

    NOTE: This regex layer is a trip-wire, NOT the primary defense.
    The real protection is the strict whitelist on LLM output (Layer 4).
    New injection patterns emerge constantly — don't over-rely on regex.
    """
    if not message:
        return message, False

    # Check for injection patterns
    for pattern in _compiled_patterns:
        if pattern.search(message):
            logger.warning(
                f"Prompt injection attempt detected: pattern={pattern.pattern}, "
                f"message_preview={message[:80]}..."
            )
            return message, True

    return message, False


def strip_injection_markers(message: str) -> str:
    """
    Remove common injection wrapper characters that try to break LLM context.
    This is applied to the message before it's embedded in the prompt template.
    """
    # Remove triple-backtick blocks (code injection)
    message = re.sub(r"```[\s\S]*?```", "[code removed]", message)
    # Remove <system>, <|im_start|>, etc. — common LLM control tokens
    message = re.sub(
        r"<\|?/?(?:system|user|assistant|im_start|im_end|endoftext)\|?>",
        "",
        message,
        flags=re.IGNORECASE,
    )
    # Remove excessive newlines (used to push instructions off-screen)
    message = re.sub(r"\n{5,}", "\n\n", message)

    return message.strip()


# ═══════════════════════════════════════════════════════════════════════════
# 3. PERSISTENT RATE LIMITER (Supabase-backed, survives restarts)
# ═══════════════════════════════════════════════════════════════════════════


class PersistentRateLimiter:
    """
    Supabase-backed rate limiter that survives service restarts.

    Uses the `rate_limits` table:
        CREATE TABLE IF NOT EXISTS rate_limits (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            key TEXT NOT NULL,          -- e.g. IP address
            attempts INT DEFAULT 1,
            window_start TIMESTAMPTZ DEFAULT now(),
            created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rate_limits_key ON rate_limits(key);

    Falls back to in-memory if Supabase is unavailable (graceful degradation).
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        # In-memory fallback in case Supabase is down
        self._fallback: dict[str, list[float]] = defaultdict(list)
        self._fallback_until: float = 0.0

    def _should_use_fallback(self) -> bool:
        return time.time() < self._fallback_until

    def _trigger_fallback(self, error: Exception) -> None:
        self._fallback_until = time.time() + 60.0  # Time-box fallback to 60s (T3.2c)
        logger.warning(
            f"RATE_LIMITER_DEGRADED: Supabase rate_limits operation failed, "
            f"using in-memory fallback for 60s: {error}"
        )

    def _get_supabase(self):
        """Lazy import to avoid circular imports."""
        try:
            from app.database import supabase

            return supabase
        except Exception:
            return None

    def is_rate_limited(self, key: str) -> bool:
        """Check if a key (e.g. IP address) has exceeded the rate limit."""
        supabase = self._get_supabase()

        if supabase and not self._should_use_fallback():
            try:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=self.window_seconds)
                ).isoformat()

                result = (
                    supabase.table("rate_limits")
                    .select("attempts, window_start")
                    .eq("key", key)
                    .gte("window_start", cutoff)
                    .execute()
                )

                if result.data:
                    return result.data[0]["attempts"] >= self.max_attempts
                return False

            except Exception as e:
                self._trigger_fallback(e)

        # In-memory fallback
        return self._fallback_is_limited(key)

    def check_and_record(self, key: str) -> bool:
        """Atomically check-and-increment the attempt count in one round trip.

        Fixes a TOCTOU race in the separate is_rate_limited()/record_attempt()
        pair: under parallelized concurrent attempts, multiple requests could
        each pass is_rate_limited() before any of their record_attempt()
        writes landed, allowing more than max_attempts within one window.

        Returns:
            True if this attempt IS rate-limited (caller should reject it).
        """
        supabase = self._get_supabase()

        if supabase and not self._should_use_fallback():
            try:
                result = supabase.rpc(
                    "check_and_record_rate_limit",
                    {
                        "p_key": key,
                        "p_max_attempts": self.max_attempts,
                        "p_window_seconds": self.window_seconds,
                    },
                ).execute()
                attempts = result.data
                if isinstance(attempts, (int, float)):
                    return attempts > self.max_attempts
                if attempts is not None:
                    raise ValueError(f"RPC returned non-integer: {type(attempts)}")
            except Exception as e:
                self._trigger_fallback(e)

        was_limited = self._fallback_is_limited(key)
        self._fallback[key].append(time.time())
        return was_limited

    def record_attempt(self, key: str) -> None:
        """Record a login attempt."""
        supabase = self._get_supabase()

        if supabase and not self._should_use_fallback():
            try:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=self.window_seconds)
                ).isoformat()

                # Check if a current window exists
                existing = (
                    supabase.table("rate_limits")
                    .select("id, attempts, window_start")
                    .eq("key", key)
                    .execute()
                )

                if existing.data:
                    row = existing.data[0]
                    window_start = row["window_start"]

                    # If the window is still active, increment
                    if window_start and window_start >= cutoff:
                        supabase.table("rate_limits").update(
                            {"attempts": row["attempts"] + 1}
                        ).eq("id", row["id"]).execute()
                    else:
                        # Window expired — reset it
                        supabase.table("rate_limits").update(
                            {
                                "attempts": 1,
                                "window_start": datetime.now(timezone.utc).isoformat(),
                            }
                        ).eq("id", row["id"]).execute()
                else:
                    # First attempt from this key
                    supabase.table("rate_limits").insert(
                        {
                            "key": key,
                            "attempts": 1,
                            "window_start": datetime.now(timezone.utc).isoformat(),
                        }
                    ).execute()

                return

            except Exception as e:
                self._trigger_fallback(e)

        # In-memory fallback
        self._fallback[key].append(time.time())

    def remaining_attempts(self, key: str) -> int:
        """How many attempts remain before rate limiting kicks in."""
        supabase = self._get_supabase()

        if supabase and not self._should_use_fallback():
            try:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=self.window_seconds)
                ).isoformat()

                result = (
                    supabase.table("rate_limits")
                    .select("attempts")
                    .eq("key", key)
                    .gte("window_start", cutoff)
                    .execute()
                )

                if result.data:
                    return max(0, self.max_attempts - result.data[0]["attempts"])
                return self.max_attempts

            except Exception as e:
                self._trigger_fallback(e)

        # In-memory fallback
        now = time.time()
        cutoff = now - self.window_seconds
        recent = [ts for ts in self._fallback[key] if ts > cutoff]
        return max(0, self.max_attempts - len(recent))

    def reset(self, key: str) -> None:
        """Reset attempts for a key (e.g. after successful login)."""
        supabase = self._get_supabase()

        if supabase and not self._should_use_fallback():
            try:
                supabase.table("rate_limits").delete().eq("key", key).execute()
                return
            except Exception as e:
                self._trigger_fallback(e)

        # In-memory fallback
        self._fallback.pop(key, None)

    def _fallback_is_limited(self, key: str) -> bool:
        """In-memory fallback rate limiter."""
        now = time.time()
        cutoff = now - self.window_seconds
        self._fallback[key] = [ts for ts in self._fallback[key] if ts > cutoff]
        return len(self._fallback[key]) >= self.max_attempts


# Global rate limiter for admin login
# Persistent via Supabase — survives Render/Railway restarts
login_rate_limiter = PersistentRateLimiter(max_attempts=5, window_seconds=60)


# ═══════════════════════════════════════════════════════════════════════════
# 4. SECURITY HEADERS
# ═══════════════════════════════════════════════════════════════════════════

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    ),
}
