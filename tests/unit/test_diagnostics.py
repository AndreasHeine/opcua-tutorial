"""Chapter 28 tests: structured logging that a log pipeline can parse.

Logging is configured once per process by :func:`~opcua_toolkit.configure_logging`
and never inside an example. The tests restore the real root logger state
afterwards so one test cannot leak handlers into the next.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from opcua_toolkit.diagnostics import JsonFormatter, configure_logging, log_with

_logger_suffix = itertools.count()


@contextmanager
def restore_logging() -> Iterator[None]:
    """Put the root logger back the way pytest found it."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_asyncua = {
        name: logging.getLogger(name).level
        for name in ("asyncua", "asyncua.client", "asyncua.server", "asyncua.common")
    }
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_asyncua.items():
            logging.getLogger(name).setLevel(level)


class Capture(logging.Handler):
    """Collect records exactly as a real logger produced them."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capturing(name: str) -> Iterator[tuple[logging.Logger, Capture]]:
    """Attach a capture handler to one logger and hand both back.

    Logger names are unique per call so two tests can never see each other's
    records, which is the failure mode that makes log tests flaky.
    """
    logger = logging.getLogger(f"opcua_toolkit.test.{name}.{next(_logger_suffix)}")
    logger.propagate = False
    sink = Capture()
    logger.handlers = [sink]
    logger.setLevel(logging.DEBUG)
    try:
        yield logger, sink
    finally:
        logger.handlers = []


def one_record(level: int = logging.INFO, message: str = "sample arrived") -> logging.LogRecord:
    with capturing("formatter") as (logger, sink):
        logger.log(level, message)
    assert sink.records
    return sink.records[0]


def test_json_formatter_emits_one_parseable_object_per_line() -> None:
    rendered = JsonFormatter().format(one_record())
    payload = json.loads(rendered)
    assert payload["level"] == "INFO"
    assert payload["message"] == "sample arrived"
    assert payload["logger"].startswith("opcua_toolkit.test.formatter")
    assert payload["timestamp"].startswith("20")
    assert "\n" not in rendered


def test_json_formatter_includes_structured_fields() -> None:
    with capturing("structured") as (logger, sink):
        log_with(logger, logging.INFO, "wrote tag", node_id="ns=2;s=Setpoint", attempts=2)
    payload = json.loads(JsonFormatter().format(sink.records[0]))
    assert payload["message"] == "wrote tag"
    assert payload["node_id"] == "ns=2;s=Setpoint"
    assert payload["attempts"] == 2


def test_json_formatter_keeps_the_exception_trace() -> None:
    def _failed_write() -> None:
        raise ValueError("BadTypeMismatch on ns=2;s=Setpoint")

    with capturing("crash") as (logger, sink):
        try:
            _failed_write()
        except ValueError:
            logger.exception("write failed")
    payload = json.loads(JsonFormatter().format(sink.records[0]))
    assert payload["level"] == "ERROR"
    assert payload["message"] == "write failed"
    assert "BadTypeMismatch" in payload["exception"]
    assert "ValueError" in payload["exception"]


def test_json_output_is_one_object_per_line_for_a_whole_run() -> None:
    with capturing("lines") as (logger, sink):
        for index in range(3):
            log_with(logger, logging.INFO, "published", sequence=index)
    rendered = "\n".join(JsonFormatter().format(record) for record in sink.records)
    decoded: list[dict[str, Any]] = [json.loads(line) for line in rendered.splitlines()]
    assert [line["sequence"] for line in decoded] == [0, 1, 2]


def test_configure_logging_quiet_libraries_by_default() -> None:
    with restore_logging():
        root = configure_logging("INFO")
        assert root.level == logging.INFO
        # asyncua logs reconnect churn at INFO; that is noise unless you are debugging.
        assert logging.getLogger("asyncua").level == logging.WARNING
        assert len(root.handlers) == 1


def test_configure_logging_can_turn_the_library_up_for_debugging() -> None:
    with restore_logging():
        configure_logging("DEBUG", library_level="DEBUG")
        assert logging.getLogger("asyncua").level == logging.DEBUG
        assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_selects_the_formatter() -> None:
    with restore_logging():
        human = configure_logging("WARNING")
        rendered = human.handlers[0].format(one_record())
        assert "sample arrived" in rendered
        assert "|" in rendered
        assert not rendered.startswith("{")

        structured = configure_logging("WARNING", json_output=True)
        assert isinstance(structured.handlers[0].formatter, JsonFormatter)
        assert json.loads(structured.handlers[0].format(one_record()))["message"] == (
            "sample arrived"
        )


def test_configuring_logging_twice_does_not_duplicate_output() -> None:
    # Two entry points in one process must not print every line twice.
    with restore_logging():
        configure_logging("INFO")
        root = configure_logging("INFO")
        assert len(root.handlers) == 1


def test_library_level_can_be_given_as_a_number() -> None:
    with restore_logging():
        configure_logging("WARNING", library_level=logging.DEBUG)
        assert logging.getLogger("asyncua").level == logging.DEBUG


def test_configure_logging_rejects_a_level_that_does_not_exist() -> None:
    with restore_logging(), pytest.raises(ValueError, match="Unknown level"):
        configure_logging("NOT A LEVEL")
