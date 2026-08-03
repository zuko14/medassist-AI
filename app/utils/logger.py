"""Structured logging configuration for MediAssist AI (Security Hardened).

Supports two output modes:
  - Production (APP_ENV=production): JSON-formatted structured logs
    for cloud log aggregation (Railway, Render, GCP, Datadog, etc.)
  - Development (all other modes): Human-readable colored console output

JSON log format includes:
  - timestamp (ISO 8601)
  - level (INFO, WARNING, ERROR, etc.)
  - logger (module name)
  - message (log text)
  - service (always "mediassist-ai")
  - version (app version)
"""

import json
import re
import logging
import sys
from datetime import datetime, timezone

from app.config import settings

_APP_VERSION = "2.0.0"
_SERVICE_NAME = "mediassist-ai"

SENSITIVE_PATTERNS = [
    (re.compile(r"(Authorization:\s*Bearer\s+)[^\s,'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(X-Signature-256:\s*)[^\s,'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(X-Integration-Secret:\s*)[^\s,'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(password[\"':\s=]+)[^\s,'\"]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Cookie:\s*)[^\r\n]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Set-Cookie:\s*)[^\r\n]+", re.IGNORECASE), r"\1[REDACTED]"),
]


def sanitize_log_message(msg: str) -> str:
    """Scrub sensitive credentials, headers, and secrets from log message strings."""
    if not isinstance(msg, str):
        return msg
    for pattern, repl in SENSITIVE_PATTERNS:
        msg = pattern.sub(repl, msg)
    return msg


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for production environments.

    Each log line is a single JSON object. Compatible with:
      - Railway log viewer
      - Render log streams
      - GCP Cloud Logging
      - Datadog / ELK stack
    """

    def format(self, record: logging.LogRecord) -> str:
        clean_msg = sanitize_log_message(record.getMessage())
        log_entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": clean_msg,
            "service": _SERVICE_NAME,
            "version": _APP_VERSION,
        }

        if record.levelno >= logging.WARNING:
            log_entry["source"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }

        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": sanitize_log_message(str(record.exc_info[1])),
                "traceback": self.formatException(record.exc_info),
            }

        return json.dumps(log_entry, default=str, ensure_ascii=False)


class ReadableFormatter(logging.Formatter):
    """Human-readable formatter for development with log-level coloring."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        reset = self.RESET if color else ""
        timestamp = datetime.now().strftime("%H:%M:%S")
        clean_msg = sanitize_log_message(record.getMessage())
        base = f"{color}{timestamp} [{record.levelname:>7}]{reset} {record.name}: {clean_msg}"
        if record.exc_info and record.exc_info[0] is not None:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging():
    """Configure structured logging based on environment.

    - Production: JSON format (one JSON object per line)
    - Development/Testing: Human-readable colored output
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    is_production = settings.app_env == "production"
    formatter = JSONFormatter() if is_production else ReadableFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    logging.basicConfig(
        level=log_level,
        handlers=[handler],
        force=True,
    )

    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance."""
    return logging.getLogger(name)
