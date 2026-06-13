# tgbridge/otel.py — optional OpenTelemetry tracing (graceful degradation).
#
# If opentelemetry-sdk + opentelemetry-exporter-otlp are NOT installed,
# every public symbol here is a no-op and the bot behaves exactly as before.
#
# Environment variables read (all optional):
#   UPTRACE_DSN                  — Uptrace project DSN, e.g.
#                                   http://<token>@localhost:14318/<project_id>
#                                   When absent/empty → tracing disabled.
#   OTEL_EXPORTER_OTLP_ENDPOINT — Override OTLP endpoint (default: http://127.0.0.1:4318
#                                   for HTTP/proto, or 4317 for gRPC).
#   OTEL_EXPORTER_OTLP_HEADERS  — Headers for OTLP exporter, e.g. "uptrace-dsn=<dsn>"
#   OTEL_SERVICE_NAME            — Override service name (default: tg-bot)
#   OTEL_TRACES_EXPORTER         — "otlp" (default when DSN present) or "none"
#
# Usage:
#   from tgbridge.otel import init_tracing, span
#
#   init_tracing()   # call once at startup
#
#   with span("my.operation", attr1="v1") as s:
#       ...  # s may be a real Span or a no-op _NoopSpan

from __future__ import annotations

import contextlib
import logging
import os
from typing import Generator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Try to import OTel SDK — optional dependency
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider as _TracerProvider
    from opentelemetry.sdk.resources import Resource as _Resource
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor as _BatchSpanProcessor,
        SpanExportResult as _SpanExportResult,
    )
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as _OTLPSpanExporter,
        )
        _OTLP_PROTO = "http"
    except ImportError:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter as _OTLPSpanExporter,  # type: ignore[no-redef]
            )
            _OTLP_PROTO = "grpc"
        except ImportError:
            _OTLPSpanExporter = None  # type: ignore[assignment,misc]
            _OTLP_PROTO = None
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    _OTLPSpanExporter = None
    _OTLP_PROTO = None

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_tracer: "object | None" = None   # real Tracer or None


# ---------------------------------------------------------------------------
# No-op span context manager (used when OTel is absent or disabled)
# ---------------------------------------------------------------------------

class _NoopSpan:
    """Minimal span interface that does nothing."""

    def set_attribute(self, key: str, value: object) -> None:  # noqa: D401
        pass

    def record_exception(self, exc: BaseException) -> None:
        pass

    def set_status(self, status: object, description: str = "") -> None:
        pass


@contextlib.contextmanager
def _noop_span(*_args: object, **_kwargs: object) -> Generator[_NoopSpan, None, None]:
    yield _NoopSpan()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_tracing() -> None:
    """Initialise the OTel tracer provider once at bot startup.

    Silently disables tracing (no-op) when:
    - opentelemetry packages are not installed, or
    - UPTRACE_DSN env var is absent/empty and no explicit OTLP endpoint is set,
    - or if any initialisation error occurs (logs a warning, bot keeps running).
    """
    global _tracer

    if not _OTEL_AVAILABLE:
        logger.debug("otel: opentelemetry packages not installed — tracing disabled")
        return

    if _OTLPSpanExporter is None:
        logger.debug("otel: no OTLP exporter available — tracing disabled")
        return

    uptrace_dsn = os.getenv("UPTRACE_DSN", "").strip()
    explicit_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

    if not uptrace_dsn and not explicit_endpoint:
        logger.debug("otel: UPTRACE_DSN and OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled")
        return

    if os.getenv("OTEL_TRACES_EXPORTER", "otlp") == "none":
        logger.debug("otel: OTEL_TRACES_EXPORTER=none — tracing disabled")
        return

    try:
        service_name = os.getenv("OTEL_SERVICE_NAME", "tg-bot")
        resource = _Resource.create({
            "service.name": service_name,
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "claude-agent-studio"),
        })

        provider = _TracerProvider(resource=resource)

        # Build exporter kwargs
        exporter_kwargs: dict = {}

        if uptrace_dsn:
            if _OTLP_PROTO == "http":
                # For HTTP exporter the endpoint must be the FULL URL including
                # /v1/traces — the Python OTLP/HTTP exporter does NOT append it
                # automatically (unlike gRPC).  _http_traces_endpoint() normalises
                # both the default and any user-supplied OTEL_EXPORTER_OTLP_ENDPOINT.
                endpoint = _http_traces_endpoint(explicit_endpoint)
                exporter_kwargs["endpoint"] = endpoint
                exporter_kwargs["headers"] = {"uptrace-dsn": uptrace_dsn}
            else:
                # gRPC exporter: endpoint without path
                endpoint = explicit_endpoint or "http://127.0.0.1:4317"
                exporter_kwargs["endpoint"] = endpoint
                exporter_kwargs["headers"] = [("uptrace-dsn", uptrace_dsn)]
        elif explicit_endpoint:
            exporter_kwargs["endpoint"] = explicit_endpoint
            raw_headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
            if raw_headers:
                exporter_kwargs["headers"] = _parse_headers(raw_headers)

        exporter = _OTLPSpanExporter(**exporter_kwargs)

        # BatchSpanProcessor with limited queue to avoid memory bloat
        bsp = _BatchSpanProcessor(
            exporter,
            max_queue_size=512,
            max_export_batch_size=64,
            schedule_delay_millis=5000,
        )
        provider.add_span_processor(bsp)
        _otel_trace.set_tracer_provider(provider)

        _tracer = _otel_trace.get_tracer(service_name)
        logger.info("otel: tracing enabled → %s (proto=%s)", exporter_kwargs.get("endpoint", ""), _OTLP_PROTO)

    except Exception as exc:  # noqa: BLE001
        logger.warning("otel: init failed (%s) — tracing disabled", exc)
        _tracer = None


def _http_traces_endpoint(explicit: str, default: str = "http://127.0.0.1:14318") -> str:
    """Return the full OTLP HTTP traces endpoint, always ending with /v1/traces.

    The Python OTLP/HTTP exporter treats the ``endpoint`` argument as a
    **complete URL** and does NOT append ``/v1/traces`` automatically (unlike
    the gRPC exporter).  Passing a bare host:port results in a POST to ``/``
    which Uptrace/OTel collectors reject with 405 Method Not Allowed.

    Args:
        explicit: Value from ``OTEL_EXPORTER_OTLP_ENDPOINT`` (may be empty).
        default:  Fallback base URL when ``explicit`` is empty.

    Returns:
        URL with ``/v1/traces`` suffix, never doubled.
    """
    base = (explicit or default).rstrip("/")
    if not base.endswith("/v1/traces"):
        base = base + "/v1/traces"
    return base


def _parse_headers(raw: str) -> dict:
    """Parse 'key=value,key2=value2' header string into a dict."""
    result: dict = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result


@contextlib.contextmanager
def span(
    name: str,
    **attrs: object,
) -> Generator[object, None, None]:
    """Context manager that wraps a named OTel span.

    When tracing is disabled (no packages / not configured), yields a _NoopSpan.

    Usage::

        with span("claude.run", mode="dev", agent="python-dev") as s:
            result = run_claude(...)
            s.set_attribute("output.len", len(result))
    """
    if _tracer is None:
        with _noop_span(name) as s:
            yield s
        return

    with _tracer.start_as_current_span(name) as otel_span:  # type: ignore[union-attr]
        for k, v in attrs.items():
            otel_span.set_attribute(k, v)
        try:
            yield otel_span
        except Exception as exc:
            try:
                otel_span.record_exception(exc)
                from opentelemetry.trace import StatusCode
                otel_span.set_status(StatusCode.ERROR, str(exc))
            except Exception:
                pass
            raise
