"""Security utilities — webhook signature verification, input sanitization, rate limiting."""

import hmac
import hashlib
import logging
import re
import time
from collections import defaultdict
from typing import Optional

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

    Args:
        payload_body: Raw request body bytes (before JSON parsing).
        signature_header: Value of X-Hub-Signature-256 header.
        app_secret: Your Meta App Secret (from Meta Developer Console).

    Returns:
        True if signature is valid, False otherwise.
    """
    if not app_secret:
        # If no app secret configured, log a warning but allow (backward compat)
        logger.warning(
            "META_APP_SECRET not configured — webhook signature verification SKIPPED. "
            "Set META_APP_SECRET in .env for production security."
        )
        return True

    if not signature_header:
        logger.warning("Webhook request missing X-Hub-Signature-256 header — REJECTED")
        return False

    # Meta format: "sha256=<hex_digest>"
    if not signature_header.startswith("sha256="):
        logger.warning("Webhook signature has invalid format — REJECTED")
        return False

    expected_signature = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected_signature, signature_header)

    if not is_valid:
        logger.warning("Webhook signature mismatch — REJECTED (possible spoofed request)")

    return is_valid


# ═══════════════════════════════════════════════════════════════════════════
# 2. LLM PROMPT INJECTION SANITIZER
# ═══════════════════════════════════════════════════════════════════════════

# Patterns that indicate prompt injection attempts
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|context)",
    r"you\s+are\s+now\s+(an?\s+)?(admin|administrator|root|superuser|developer|hacker)",
    r"system\s*:\s*",                          # Trying to inject system-role messages
    r"act\s+as\s+(if\s+)?(an?\s+)?(admin|root|developer|hacker|unrestricted)",
    r"(show|display|list|reveal|dump|print)\s+(all|every|my)?\s*(patient|appointment|record|data|database|secret|key|token|password)",
    r"forget\s+(all\s+)?(your\s+)?(rules?|instructions?|training|constraints?)",
    r"(bypass|override|disable|skip|break)\s+(security|restrictions?|rules?|filters?|safety)",
    r"pretend\s+(you\s+)?(are|to\s+be)\s+(not\s+)?(a|an)?\s*(ai|bot|assistant|chatbot)",
    r"execute\s+(this\s+)?(sql|query|command|code|script)",
    r"(drop|delete|truncate|alter)\s+table",   # SQL injection in chat
    r"\{[^}]*role\s*:\s*['\"]?system",         # JSON role injection
]

_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def sanitize_user_input(message: str) -> tuple[str, bool]:
    """
    Sanitize user input before passing to LLM.

    Returns:
        (sanitized_message, is_suspicious) — the cleaned message and whether
        prompt injection was detected.
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
    message = re.sub(r"<\|?/?(?:system|user|assistant|im_start|im_end|endoftext)\|?>", "", message, flags=re.IGNORECASE)
    # Remove excessive newlines (used to push instructions off-screen)
    message = re.sub(r"\n{5,}", "\n\n", message)

    return message.strip()


# ═══════════════════════════════════════════════════════════════════════════
# 3. IN-MEMORY RATE LIMITER (for admin login brute-force protection)
# ═══════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """
    Simple in-memory sliding-window rate limiter.
    Good enough for single-instance deployments on Railway/Render.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        # {key: [timestamp, timestamp, ...]}
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def is_rate_limited(self, key: str) -> bool:
        """Check if a key (e.g. IP address) has exceeded the rate limit."""
        now = time.time()
        cutoff = now - self.window_seconds

        # Prune old attempts
        self._attempts[key] = [
            ts for ts in self._attempts[key] if ts > cutoff
        ]

        return len(self._attempts[key]) >= self.max_attempts

    def record_attempt(self, key: str) -> None:
        """Record an attempt for a key."""
        self._attempts[key].append(time.time())

    def remaining_attempts(self, key: str) -> int:
        """How many attempts remain before rate limiting kicks in."""
        now = time.time()
        cutoff = now - self.window_seconds
        recent = [ts for ts in self._attempts[key] if ts > cutoff]
        return max(0, self.max_attempts - len(recent))

    def reset(self, key: str) -> None:
        """Reset attempts for a key (e.g. after successful login)."""
        self._attempts.pop(key, None)


# Global rate limiter for admin login
login_rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)


# ═══════════════════════════════════════════════════════════════════════════
# 4. SECURITY HEADERS
# ═══════════════════════════════════════════════════════════════════════════

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    ),
}
