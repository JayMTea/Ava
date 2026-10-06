"""Tracing, metrics, audit and logging, configured by observability/*.yaml.

Spans are always written as JSON lines to state/sessions/<session_id>/trace.jsonl, using
OpenTelemetry GenAI semantic-convention attribute names (gen_ai.*) so the same data can
be exported to any OTel backend by setting `exporter: otlp` in observability/tracing.yaml.

Prompt and completion text is NOT recorded unless tracing.yaml sets capture_content: true.
Everything written passes through the safety.yaml redaction patterns first.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_current_span: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("span", default=None)
log = logging.getLogger("agent_platform")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class JsonlWriter:
    def __init__(self, path: Path, redact: Callable[[str], str]):
        self.path = path
        self.redact = redact

    def write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = self.redact(json.dumps(record, default=str, ensure_ascii=False))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class Telemetry:
    def __init__(self, config: dict[str, dict[str, Any]], session_dir: Path, redact: Callable[[str], str]):
        self.tracing = config.get("tracing", {})
        self.metrics_cfg = config.get("metrics", {})
        self.enabled = self.tracing.get("enabled", True)
        self.capture_content = self.tracing.get("capture_content", False)
        self.redact = redact
        self.trace_log = JsonlWriter(session_dir / "trace.jsonl", redact)
        self.metric_log = JsonlWriter(session_dir / "metrics.jsonl", redact)
        self.audit_log = JsonlWriter(session_dir / "audit.jsonl", redact)
        self._otel = self._init_otel() if self.tracing.get("exporter") == "otlp" else None

    def _init_otel(self) -> Any:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            log.warning("tracing.yaml asks for exporter: otlp but opentelemetry is not installed (extra: otel)")
            return None
        provider = TracerProvider(resource=Resource.create({"service.name": self.tracing.get("service_name", "agent-platform")}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # endpoint from OTEL_EXPORTER_OTLP_ENDPOINT
        trace.set_tracer_provider(provider)
        return trace.get_tracer("agent_platform")

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
        """Yield a mutable attribute dict; callers add attributes as they learn them."""
        parent = _current_span.get()
        record: dict[str, Any] = {
            "trace_id": parent["trace_id"] if parent else uuid.uuid4().hex,
            "span_id": uuid.uuid4().hex[:16],
            "parent_id": parent["span_id"] if parent else None,
            "name": name,
            "start": utcnow(),
            "attributes": dict(attributes),
            "status": "ok",
        }
        token = _current_span.set(record)
        started = time.perf_counter()
        otel_cm = self._otel.start_as_current_span(name) if self._otel else None
        otel_span = otel_cm.__enter__() if otel_cm else None
        try:
            yield record["attributes"]
        except BaseException as exc:
            record["status"] = "error"
            record["attributes"]["error.type"] = type(exc).__name__
            raise
        finally:
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            _current_span.reset(token)
            if otel_span is not None:
                for key, value in record["attributes"].items():
                    if isinstance(value, (str, bool, int, float)):
                        otel_span.set_attribute(key, value)
                otel_cm.__exit__(None, None, None)
            if self.enabled:
                self.trace_log.write(record)

    def metric(self, name: str, value: float, **attributes: Any) -> None:
        if self.metrics_cfg.get("enabled", True):
            self.metric_log.write({"time": utcnow(), "name": name, "value": value, "attributes": attributes})

    def audit(self, event: str, **fields: Any) -> None:
        """Security-relevant events (policy denials, approvals). Always on - not sampled."""
        self.audit_log.write({"time": utcnow(), "event": event, **fields})

    def content(self, text: str) -> str | None:
        """Return text for a span attribute only when content capture is enabled."""
        return text if self.capture_content else None


def configure_logging(config: dict[str, Any], redact: Callable[[str], str]) -> None:
    level = getattr(logging, str(config.get("level", "INFO")).upper(), logging.INFO)

    class RedactingFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            if config.get("format", "json") == "json":
                payload = {"time": utcnow(), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
                return redact(json.dumps(payload, ensure_ascii=False))
            return redact(super().format(record))

    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("agent_platform")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
