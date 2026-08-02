"""Tests for structured logging module (app/utils/logger.py).

Verifies:
  - JSON formatter produces valid JSON output
  - JSON format includes required fields (timestamp, level, logger, message, service, version)
  - Error logs include source location
  - Exception info is captured in JSON output
  - ReadableFormatter produces non-JSON human-readable output
  - setup_logging runs without error in test environment
"""

import json
import logging

from app.utils.logger import JSONFormatter, ReadableFormatter, setup_logging, get_logger


class TestJSONFormatter:
    """Tests for production JSON log formatter."""

    def _make_record(
        self, message="Test message", level=logging.INFO, name="test.module"
    ):
        """Create a LogRecord for testing."""
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname="test_file.py",
            lineno=42,
            msg=message,
            args=(),
            exc_info=None,
        )
        return record

    def test_produces_valid_json(self):
        formatter = JSONFormatter()
        record = self._make_record()
        output = formatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_contains_required_fields(self):
        formatter = JSONFormatter()
        record = self._make_record(message="Hello MediAssist")
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.module"
        assert parsed["message"] == "Hello MediAssist"
        assert parsed["service"] == "mediassist-ai"
        assert parsed["version"] == "2.0.0"

    def test_warning_includes_source_location(self):
        formatter = JSONFormatter()
        record = self._make_record(level=logging.WARNING, message="Rate limit hit")
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "source" in parsed
        assert parsed["source"]["file"] == "test_file.py"
        assert parsed["source"]["line"] == 42

    def test_error_includes_source_location(self):
        formatter = JSONFormatter()
        record = self._make_record(level=logging.ERROR, message="DB down")
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "source" in parsed
        assert "function" in parsed["source"]

    def test_info_excludes_source_location(self):
        formatter = JSONFormatter()
        record = self._make_record(level=logging.INFO)
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "source" not in parsed

    def test_exception_info_captured(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("test error for logging")
        except ValueError:
            import sys

            record = self._make_record(level=logging.ERROR, message="Caught error")
            record.exc_info = sys.exc_info()

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "exception" in parsed
        assert parsed["exception"]["type"] == "ValueError"
        assert "test error for logging" in parsed["exception"]["message"]
        assert "traceback" in parsed["exception"]

    def test_unicode_handling(self):
        """Hindi/Telugu characters should be preserved in JSON (ensure_ascii=False)."""
        formatter = JSONFormatter()
        record = self._make_record(
            message="Clinical firewall triggered: दवा keyword detected"
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "दवा" in parsed["message"]


class TestReadableFormatter:
    """Tests for development human-readable formatter."""

    def _make_record(self, message="Dev message", level=logging.INFO, name="test"):
        record = logging.LogRecord(
            name=name,
            level=level,
            pathname="test.py",
            lineno=10,
            msg=message,
            args=(),
            exc_info=None,
        )
        return record

    def test_produces_readable_string(self):
        formatter = ReadableFormatter()
        record = self._make_record()
        output = formatter.format(record)

        # Should NOT be valid JSON
        is_json = True
        try:
            json.loads(output)
        except (json.JSONDecodeError, ValueError):
            is_json = False
        assert not is_json

        assert "Dev message" in output

    def test_includes_level_name(self):
        formatter = ReadableFormatter()
        record = self._make_record(level=logging.WARNING, message="Warn test")
        output = formatter.format(record)
        assert "WARNING" in output

    def test_includes_logger_name(self):
        formatter = ReadableFormatter()
        record = self._make_record(name="app.services.firewall")
        output = formatter.format(record)
        assert "app.services.firewall" in output


class TestSetupLogging:
    """Tests for setup_logging() initialization."""

    def test_setup_runs_without_error(self):
        """setup_logging should complete without raising any exception."""
        setup_logging()

    def test_get_logger_returns_logger(self):
        logger = get_logger("test.module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.module"
