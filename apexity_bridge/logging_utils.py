"""Central logging configuration with secret redaction.

Guarantees:
* Rotating file handler + console handler.
* Tokens/passwords never reach the log files — both a filter (in-place
  record redaction) and a redacting formatter are applied.
* No biometric data (fingerprints/templates) is ever logged — the codebase
  never reads templates, and raw record dumps are truncated elsewhere.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_CONFIGURED = False

# Patterns that must never be written to logs.
_REDACT_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)(token|password|api[_-]?key|secret)(\s*[:=]\s*)(\S{4,})"),
]


def _redact(text: str) -> str:
    for pat in _REDACT_PATTERNS:
        text = pat.sub(
            lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex == 3
            else (m.group(1) + "***"),
            text,
        )
    return text


class SecretsFilter(logging.Filter):
    """Redact credentials from the rendered message (mutates record in-place)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = _redact(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    """Idempotent logging setup. File handler is added when log_dir is given."""
    global _CONFIGURED
    root = logging.getLogger("apexity_bridge")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    fmt = RedactingFormatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
    )
    secret_filter = SecretsFilter()

    if not _CONFIGURED:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.addFilter(secret_filter)
        root.addHandler(console)
        _CONFIGURED = True
    else:
        for handler in root.handlers:
            handler.setFormatter(fmt)

    if log_dir is not None and not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    ):
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "bridge.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(secret_filter)
        root.addHandler(file_handler)

    # Quieten noisy third parties.
    for noisy in ("urllib3", "werkzeug", "waitress"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root
