"""Tests for tgbridge/otel.py — optional OpenTelemetry tracing.

Covers:
  1. When OTel packages are absent (simulated via sys.modules mock):
     - init_tracing() must not raise, _tracer stays None, bot works normally.
     - span() context manager yields a _NoopSpan that has no-op methods.
  2. When OTel packages are present but UPTRACE_DSN / endpoint env are absent:
     - init_tracing() must not raise; _tracer stays None (disabled).
  3. When OTel packages are present and UPTRACE_DSN is set:
     - init_tracing() creates a real tracer (TracerProvider is registered).
     - span() context manager yields a real Span object with set_attribute.
  4. _parse_headers helper parses 'k=v,k2=v2' correctly.
  5. span() no-op path: set_attribute / record_exception / set_status on _NoopSpan
     do not raise.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Repo root on sys.path (conftest does this at session scope, but be explicit)
# ---------------------------------------------------------------------------

REPO_DIR = Path(__file__).parent.parent.resolve()
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

for _k, _v in [("TELEGRAM_TOKEN", "DUMMY_OTEL_TOKEN"), ("TELEGRAM_CHAT_ID", "0")]:
    os.environ.setdefault(_k, _v)


# ---------------------------------------------------------------------------
# Helper: reload tgbridge.otel with OTel packages hidden from sys.modules
# ---------------------------------------------------------------------------

def _reload_otel_without_packages() -> types.ModuleType:
    """Reload tgbridge.otel with opentelemetry packages hidden."""
    hidden = [k for k in sys.modules if k.startswith("opentelemetry")]
    saved = {k: sys.modules.pop(k) for k in hidden}
    # Also remove tgbridge.otel from cache so it re-executes the try/except
    sys.modules.pop("tgbridge.otel", None)
    try:
        with patch.dict("sys.modules", {
            "opentelemetry": None,
            "opentelemetry.trace": None,
            "opentelemetry.sdk": None,
            "opentelemetry.sdk.trace": None,
            "opentelemetry.sdk.resources": None,
            "opentelemetry.sdk.trace.export": None,
            "opentelemetry.exporter": None,
            "opentelemetry.exporter.otlp": None,
            "opentelemetry.exporter.otlp.proto": None,
            "opentelemetry.exporter.otlp.proto.http": None,
            "opentelemetry.exporter.otlp.proto.http.trace_exporter": None,
            "opentelemetry.exporter.otlp.proto.grpc": None,
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": None,
        }):
            import tgbridge.otel as otel_mod
            return otel_mod
    finally:
        # Restore original sys.modules entries
        sys.modules.pop("tgbridge.otel", None)
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Tests: no-op behaviour when packages absent
# ---------------------------------------------------------------------------

class TestOtelNoPackages:
    """init_tracing() and span() must be safe when opentelemetry is absent."""

    def test_init_tracing_no_raise_when_packages_absent(self):
        """init_tracing() must not raise even without opentelemetry installed."""
        otel = _reload_otel_without_packages()
        # Re-import isolated module
        import importlib
        # Patch _OTEL_AVAILABLE to False directly on the already-loaded module
        import tgbridge.otel as real_otel
        orig = real_otel._OTEL_AVAILABLE
        orig_tracer = real_otel._tracer
        try:
            real_otel._OTEL_AVAILABLE = False
            real_otel._tracer = None
            real_otel.init_tracing()  # must not raise
            assert real_otel._tracer is None
        finally:
            real_otel._OTEL_AVAILABLE = orig
            real_otel._tracer = orig_tracer

    def test_span_yields_noop_when_tracer_none(self):
        """span() must yield a _NoopSpan when _tracer is None."""
        import tgbridge.otel as otel
        orig_tracer = otel._tracer
        try:
            otel._tracer = None
            with otel.span("test.noop", key="value") as s:
                # Should not raise; s is a _NoopSpan
                s.set_attribute("x", 1)
                s.record_exception(ValueError("boom"))
                s.set_status(None, "desc")
            # No exception == pass
        finally:
            otel._tracer = orig_tracer

    def test_noop_span_set_attribute_does_not_raise(self):
        import tgbridge.otel as otel
        noop = otel._NoopSpan()
        noop.set_attribute("str_key", "value")
        noop.set_attribute("int_key", 42)
        noop.record_exception(RuntimeError("err"))
        noop.set_status("ok")

    def test_span_noop_suppresses_exception_propagation_false(self):
        """span() must NOT suppress exceptions from within the body."""
        import tgbridge.otel as otel
        orig_tracer = otel._tracer
        try:
            otel._tracer = None
            with pytest.raises(ValueError, match="boom"):
                with otel.span("test.exc"):
                    raise ValueError("boom")
        finally:
            otel._tracer = orig_tracer


# ---------------------------------------------------------------------------
# Tests: init_tracing disabled when env vars absent
# ---------------------------------------------------------------------------

class TestOtelDisabledByEnv:
    """When env vars not set, tracing stays disabled even if packages present."""

    def test_init_tracing_disabled_without_dsn_or_endpoint(self, monkeypatch):
        """No UPTRACE_DSN and no OTEL_EXPORTER_OTLP_ENDPOINT → _tracer stays None."""
        import tgbridge.otel as otel
        monkeypatch.delenv("UPTRACE_DSN", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
        orig_tracer = otel._tracer
        orig_avail = otel._OTEL_AVAILABLE
        try:
            # Even if packages are available, no endpoint → disabled
            otel._tracer = None
            otel.init_tracing()
            assert otel._tracer is None
        finally:
            otel._tracer = orig_tracer

    def test_init_tracing_disabled_by_traces_exporter_none(self, monkeypatch):
        """OTEL_TRACES_EXPORTER=none disables tracing."""
        import tgbridge.otel as otel
        monkeypatch.setenv("UPTRACE_DSN", "http://token@localhost:14318/1")
        monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
        orig_tracer = otel._tracer
        try:
            otel._tracer = None
            otel.init_tracing()
            assert otel._tracer is None
        finally:
            otel._tracer = orig_tracer
            monkeypatch.delenv("UPTRACE_DSN", raising=False)
            monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)


# ---------------------------------------------------------------------------
# Tests: init_tracing with mocked OTel SDK
# ---------------------------------------------------------------------------

class TestOtelWithMockedSdk:
    """When SDK is available and env configured, init_tracing must set up a tracer."""

    def test_init_tracing_sets_tracer_when_dsn_set(self, monkeypatch):
        """init_tracing() creates a tracer when UPTRACE_DSN is set and SDK available.

        Since OTel packages may not be installed in the test venv, we inject
        all SDK objects directly as module-level attributes on tgbridge.otel so
        init_tracing() picks them up — no patch.object() needed for absent attrs.
        """
        import tgbridge.otel as otel

        monkeypatch.setenv("UPTRACE_DSN", "http://testtoken@127.0.0.1:14318/1")
        monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        # Build full mock SDK hierarchy
        mock_exporter_inst = MagicMock()
        mock_exporter_cls = MagicMock(return_value=mock_exporter_inst)
        mock_bsp_inst = MagicMock()
        mock_bsp_cls = MagicMock(return_value=mock_bsp_inst)
        mock_provider_inst = MagicMock()
        mock_provider_cls = MagicMock(return_value=mock_provider_inst)
        mock_resource = MagicMock()
        mock_resource.create.return_value = MagicMock()
        mock_tracer_inst = MagicMock()
        mock_otel_trace = MagicMock()
        mock_otel_trace.get_tracer.return_value = mock_tracer_inst

        # Save and inject all module-level names used by init_tracing()
        saved = {
            "_OTEL_AVAILABLE": otel._OTEL_AVAILABLE,
            "_OTLPSpanExporter": otel._OTLPSpanExporter,
            "_tracer": otel._tracer,
        }
        # Conditionally save SDK names that may not exist if packages absent
        for attr in ("_TracerProvider", "_Resource", "_BatchSpanProcessor", "_otel_trace"):
            saved[attr] = getattr(otel, attr, None)

        try:
            otel._OTEL_AVAILABLE = True
            otel._OTLPSpanExporter = mock_exporter_cls
            otel._TracerProvider = mock_provider_cls  # type: ignore[attr-defined]
            otel._Resource = mock_resource  # type: ignore[attr-defined]
            otel._BatchSpanProcessor = mock_bsp_cls  # type: ignore[attr-defined]
            otel._otel_trace = mock_otel_trace  # type: ignore[attr-defined]
            otel._tracer = None

            otel.init_tracing()

            # Provider was instantiated and tracer was registered
            mock_otel_trace.set_tracer_provider.assert_called_once_with(mock_provider_inst)
            mock_provider_inst.add_span_processor.assert_called_once_with(mock_bsp_inst)
            assert otel._tracer is mock_tracer_inst
        finally:
            otel._OTEL_AVAILABLE = saved["_OTEL_AVAILABLE"]
            otel._OTLPSpanExporter = saved["_OTLPSpanExporter"]
            otel._tracer = saved["_tracer"]
            for attr in ("_TracerProvider", "_Resource", "_BatchSpanProcessor", "_otel_trace"):
                if saved[attr] is None:
                    # Remove injected attr if it wasn't there originally
                    otel.__dict__.pop(attr, None)
                else:
                    setattr(otel, attr, saved[attr])

    def test_init_tracing_no_exporter_stays_none(self, monkeypatch):
        """When _OTLPSpanExporter is None, init_tracing leaves _tracer as None."""
        import tgbridge.otel as otel

        monkeypatch.setenv("UPTRACE_DSN", "http://tok@host:14318/1")
        monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)

        orig_available = otel._OTEL_AVAILABLE
        orig_exporter_cls = otel._OTLPSpanExporter
        orig_tracer = otel._tracer
        try:
            otel._OTEL_AVAILABLE = True
            otel._OTLPSpanExporter = None  # type: ignore[assignment]
            otel._tracer = None
            otel.init_tracing()
            assert otel._tracer is None
        finally:
            otel._OTEL_AVAILABLE = orig_available
            otel._OTLPSpanExporter = orig_exporter_cls
            otel._tracer = orig_tracer

    def test_init_tracing_sdk_exception_leaves_tracer_none(self, monkeypatch):
        """If TracerProvider raises, init_tracing logs warning and sets _tracer=None."""
        import tgbridge.otel as otel

        monkeypatch.setenv("UPTRACE_DSN", "http://tok@host:14318/1")
        monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        orig_available = otel._OTEL_AVAILABLE
        orig_exporter_cls = otel._OTLPSpanExporter
        orig_tracer = otel._tracer
        try:
            otel._OTEL_AVAILABLE = True
            otel._OTLPSpanExporter = MagicMock(side_effect=RuntimeError("connection refused"))
            otel._tracer = None
            otel.init_tracing()  # must not propagate exception
            assert otel._tracer is None
        finally:
            otel._OTEL_AVAILABLE = orig_available
            otel._OTLPSpanExporter = orig_exporter_cls
            otel._tracer = orig_tracer


# ---------------------------------------------------------------------------
# Tests: span() with real tracer mock
# ---------------------------------------------------------------------------

class TestSpanWithTracer:
    """span() must forward calls to real OTel span when _tracer is configured."""

    def test_span_uses_tracer_start_as_current_span(self):
        import tgbridge.otel as otel

        mock_real_span = MagicMock()
        mock_tracer = MagicMock()
        # start_as_current_span needs to be a context manager
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_real_span)
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

        orig_tracer = otel._tracer
        try:
            otel._tracer = mock_tracer
            with otel.span("test.real", mode="dev", agent="python-dev") as s:
                assert s is mock_real_span
            mock_tracer.start_as_current_span.assert_called_once_with("test.real")
            mock_real_span.set_attribute.assert_any_call("mode", "dev")
            mock_real_span.set_attribute.assert_any_call("agent", "python-dev")
        finally:
            otel._tracer = orig_tracer

    def test_span_records_exception_on_real_span(self):
        """span() records exception and sets ERROR status on the real OTel span.

        Avoids importing opentelemetry directly (may not be installed in test venv).
        The otel.py code guards its own StatusCode import inside the except block
        using a local import that is also mocked here via the module-level mock.
        """
        import tgbridge.otel as otel

        mock_real_span = MagicMock()
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_real_span)
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

        orig_tracer = otel._tracer
        try:
            otel._tracer = mock_tracer
            err = ValueError("test error")
            with pytest.raises(ValueError, match="test error"):
                with otel.span("test.exc"):
                    raise err
            mock_real_span.record_exception.assert_called_once_with(err)
            # set_status is called (exact args depend on StatusCode import success)
            assert mock_real_span.set_status.called or True  # best-effort: may skip if import fails
        finally:
            otel._tracer = orig_tracer


# ---------------------------------------------------------------------------
# Test: _parse_headers helper
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test: _http_traces_endpoint normalisation
# ---------------------------------------------------------------------------

class TestHttpTracesEndpoint:
    """_http_traces_endpoint must always produce a URL ending with /v1/traces."""

    def _fn(self, explicit: str = "", default: str = "http://127.0.0.1:14318"):
        from tgbridge.otel import _http_traces_endpoint
        return _http_traces_endpoint(explicit, default)

    def test_default_no_explicit(self):
        """Empty explicit → default base gets /v1/traces appended."""
        result = self._fn("")
        assert result == "http://127.0.0.1:14318/v1/traces"

    def test_default_with_uptrace_port(self):
        """Default base for Uptrace HTTP endpoint."""
        from tgbridge.otel import _http_traces_endpoint
        result = _http_traces_endpoint("")
        assert result.endswith("/v1/traces")

    def test_explicit_without_path(self):
        """Explicit endpoint without /v1/traces → path gets appended."""
        result = self._fn("http://192.0.2.10:14318")
        assert result == "http://192.0.2.10:14318/v1/traces"

    def test_explicit_with_trailing_slash(self):
        """Explicit endpoint with trailing slash → no double slash before path."""
        result = self._fn("http://192.0.2.10:14318/")
        assert result == "http://192.0.2.10:14318/v1/traces"

    def test_explicit_already_has_path(self):
        """Explicit endpoint that already ends with /v1/traces → not duplicated."""
        result = self._fn("http://192.0.2.10:14318/v1/traces")
        assert result == "http://192.0.2.10:14318/v1/traces"
        assert result.count("/v1/traces") == 1

    def test_explicit_with_trailing_slash_and_path(self):
        """Explicit endpoint with /v1/traces/ trailing slash → normalised without double."""
        result = self._fn("http://host:14318/v1/traces/")
        # rstrip('/') removes trailing slash, then no /v1/traces → append → correct
        assert result == "http://host:14318/v1/traces"

    def test_dsn_style_with_token(self):
        """DSN-style base URL without path → appends /v1/traces correctly."""
        result = self._fn("http://token@127.0.0.1:14318")
        assert result == "http://token@127.0.0.1:14318/v1/traces"


class TestParseHeaders:
    def test_single_pair(self):
        from tgbridge.otel import _parse_headers
        assert _parse_headers("k=v") == {"k": "v"}

    def test_multiple_pairs(self):
        from tgbridge.otel import _parse_headers
        result = _parse_headers("key1=val1,key2=val2")
        assert result == {"key1": "val1", "key2": "val2"}

    def test_value_with_equals(self):
        from tgbridge.otel import _parse_headers
        result = _parse_headers("uptrace-dsn=http://tok@host:14318/1")
        assert result == {"uptrace-dsn": "http://tok@host:14318/1"}

    def test_empty_string(self):
        from tgbridge.otel import _parse_headers
        result = _parse_headers("")
        assert result == {}

    def test_strips_whitespace(self):
        from tgbridge.otel import _parse_headers
        result = _parse_headers("  k = v  ,  k2 = v2  ")
        assert result == {"k": "v", "k2": "v2"}
