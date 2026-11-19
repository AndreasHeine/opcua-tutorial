"""Logging configuration shared by examples, tools and services.

Chapter 28 builds on this module. Even at tutorial stage we never configure
logging inside an example: every entry point calls :func:`configure_logging`
once, and libraries keep their own levels.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LOGGERS_WITH_NOISY_DEFAULTS = ("asyncua", "asyncua.client", "asyncua.server", "asyncua.common")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, ready for Loki, Elasticsearch or CloudWatch."""

    def format(self, record: logging.LogRecord) -> str:
        """Render one log record as a single JSON object."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = getattr(record, "extra_fields", None)
        if isinstance(extras, dict):
            payload.update(extras)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    level: int | str = "INFO",
    *,
    json_output: bool = False,
    library_level: int | str | None = None,
) -> logging.Logger:
    """Configure the root logger exactly once per process.

    Args:
        level: Level for application loggers.
        json_output: Emit JSON lines instead of human readable text.
        library_level: Optional level for asyncua loggers. ``None`` keeps
            asyncua at ``WARNING``, which is the right default: asyncua logs
            reconnect churn at INFO and protocol detail at DEBUG.

    Returns:
        The root logger, so callers can log immediately.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter(_FORMAT, _DATE_FORMAT)
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in _LOGGERS_WITH_NOISY_DEFAULTS:
        logging.getLogger(name).setLevel(
            logging.WARNING if library_level is None else library_level
        )
    return root


def log_with(
    logger: logging.Logger,
    level: int,
    message: str,
    fields: Mapping[str, Any] | None = None,
    **extra_fields: Any,
) -> None:
    """Log with structured fields attached (rendered by :class:`JsonFormatter`).

    Args:
        logger: Logger to write to.
        level: Level of the record.
        message: Human readable message.
        fields: Structured fields as a mapping. Use this form when the field
            names come from a model (``status.as_dict()``) because splatting
            such a mapping can collide with this function's own parameters.
        extra_fields: Structured fields written inline.
    """
    merged: dict[str, Any] = {**(fields or {}), **extra_fields}
    logger.log(level, message, extra={"extra_fields": merged})


__all__ = ["JsonFormatter", "configure_logging", "log_with"]
