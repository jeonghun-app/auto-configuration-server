"""Structured logging and CloudWatch metrics.

Logs are single-line JSON on stdout so the ECS ``awslogs`` driver ships them to
CloudWatch Logs unmodified. Metrics are emitted as CloudWatch Embedded Metric
Format (EMF) documents on stdout — no ``PutMetricData`` call, therefore no extra
IAM permission, no throttling and no latency on the request path.

Metric dimensions are deliberately low-cardinality: subscriber identifiers are
never used as dimensions.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from collections.abc import MutableMapping
from typing import Any

from acs.config import Settings
from acs.security.pii import redact_mapping

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

EXTRA_KEY = "acs_extra"
"""Namespace under which structured fields travel on the LogRecord.

``logging`` refuses any ``extra`` key that collides with a built-in LogRecord
attribute (``created``, ``module``, ``name``, ``args``, ...) and raises
``KeyError`` at call time. Since the field names here come from domain data, that
would turn a logging statement into a request failure. Wrapping every field in
one reserved key removes the whole class of collision.
"""


class JsonFormatter(logging.Formatter):
    """Render log records as one-line JSON, including the correlation id."""

    def __init__(self, service_name: str, pii_mode: str, pii_secret: str) -> None:
        super().__init__()
        self._service = service_name
        self._pii_mode = pii_mode
        self._pii_secret = pii_secret

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, EXTRA_KEY, None)
        if isinstance(fields, dict) and fields:
            payload.update(redact_mapping(fields, self._pii_mode, self._pii_secret))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class StructuredLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Logger that accepts arbitrary ``extra`` field names safely."""

    def process(
        self, msg: object, kwargs: MutableMapping[str, Any]
    ) -> tuple[object, MutableMapping[str, Any]]:
        fields = kwargs.pop("extra", None) or {}
        kwargs["extra"] = {EXTRA_KEY: dict(fields)}
        return msg, kwargs


def configure_logging(settings: Settings) -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(settings.service_name, settings.pii_log_mode, settings.pii_hash_secret)
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    # uvicorn's access log would contain the full query string (IMSI, OTP,
    # token). It is disabled in the Dockerfile; silence it here too.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(logging.getLogger(name), {})


class Metrics:
    """Emit CloudWatch EMF metric documents on stdout."""

    def __init__(self, namespace: str, environment: str) -> None:
        self._namespace = namespace
        self._environment = environment

    def emit(
        self,
        name: str,
        value: float,
        unit: str = "Count",
        dimensions: dict[str, str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        dims = {"Environment": self._environment}
        if dimensions:
            dims.update(dimensions)
        doc: dict[str, Any] = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": [list(dims.keys())],
                        "Metrics": [{"Name": name, "Unit": unit}],
                    }
                ],
            },
            name: value,
            **dims,
        }
        if properties:
            doc.update(properties)
        sys.stdout.write(json.dumps(doc, default=str) + "\n")
        sys.stdout.flush()


_metrics: Metrics | None = None


def get_metrics(settings: Settings) -> Metrics:
    global _metrics  # noqa: PLW0603
    if _metrics is None:
        _metrics = Metrics(settings.metrics_namespace, settings.env)
    return _metrics


def reset_metrics() -> None:
    """Test hook."""
    global _metrics  # noqa: PLW0603
    _metrics = None
