// Package otel initialises the OpenTelemetry SDK for task-router.
//
// Graceful degradation: if Uptrace is unreachable or OTEL env vars are absent,
// the service starts normally with a no-op tracer. OTLP export failures are
// silently dropped by the SDK batch processor.
package otel

import (
	"context"
	"log/slog"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

const (
	defaultEndpoint    = ""
	defaultServiceName = "task-router"
)

// Shutdown is returned by Init and must be called on process exit to flush
// pending spans. It is a no-op when tracing is disabled.
type Shutdown func(ctx context.Context) error

// Init configures the global OTel tracer provider and returns a Shutdown func.
// Configuration is taken from standard OTEL_* env vars:
//
//	OTEL_EXPORTER_OTLP_ENDPOINT  — gRPC endpoint (e.g. http://uptrace:4317)
//	OTEL_SERVICE_NAME             — service name (default: "task-router")
//	OTEL_RESOURCE_ATTRIBUTES      — extra resource attrs (k=v,k=v)
//	UPTRACE_PROJECT_TOKEN         — Uptrace project token (sets uptrace-dsn header)
//
// If OTEL_EXPORTER_OTLP_ENDPOINT is empty, Init returns a no-op Shutdown and
// leaves the global tracer as the default no-op implementation.
func Init(log *slog.Logger) (Shutdown, error) {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		log.Warn("OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled")
		return func(ctx context.Context) error { return nil }, nil
	}

	serviceName := os.Getenv("OTEL_SERVICE_NAME")
	if serviceName == "" {
		serviceName = defaultServiceName
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Build OTLP/gRPC exporter options.
	opts := []otlptracegrpc.Option{
		otlptracegrpc.WithEndpointURL(endpoint),
		otlptracegrpc.WithInsecure(),
		otlptracegrpc.WithTimeout(5 * time.Second),
	}

	// Uptrace DSN authentication: inject uptrace-dsn metadata header.
	token := os.Getenv("UPTRACE_PROJECT_TOKEN")
	if token != "" {
		// Uptrace authenticates ingest via the uptrace-dsn gRPC metadata header.
		// Project id=1 is the first (and only) project seeded in uptrace.yml.
		opts = append(opts, otlptracegrpc.WithHeaders(map[string]string{
			"uptrace-dsn": "http://" + token + "@uptrace:14318/1",
		}))
	}

	exp, err := otlptracegrpc.New(ctx, opts...)
	if err != nil {
		// Non-fatal: Uptrace may be temporarily unavailable. Log and disable.
		log.Warn("OTel OTLP exporter init failed — tracing disabled", "err", err)
		return func(ctx context.Context) error { return nil }, nil //nolint:nilerr
	}

	res := resource.NewWithAttributes(
		semconv.SchemaURL,
		semconv.ServiceName(serviceName),
		semconv.ServiceNamespace("agent-vm"),
		semconv.DeploymentEnvironment("prod"),
	)

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(
			exp,
			sdktrace.WithMaxQueueSize(512),
			sdktrace.WithMaxExportBatchSize(64),
			sdktrace.WithExportTimeout(5*time.Second),
		),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())),
	)
	otel.SetTracerProvider(tp)

	log.Info("OpenTelemetry initialised", "service", serviceName, "endpoint", endpoint)

	return tp.Shutdown, nil
}
