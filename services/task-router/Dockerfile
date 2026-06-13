# syntax=docker/dockerfile:1
# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM golang:1.25-alpine AS builder

WORKDIR /src

# Cache module downloads separately from source
COPY go.mod go.sum ./
RUN go mod download

# Copy source and build a fully-static binary (modernc.org/sqlite = pure Go, no cgo)
COPY . .
RUN CGO_ENABLED=0 GOFLAGS="-trimpath" \
    go build -ldflags="-s -w" -o /task-router ./cmd/task-router

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM alpine:3.21

# wget is available in alpine – used for the HEALTHCHECK
RUN apk add --no-cache wget && \
    addgroup -S app && adduser -S -G app app

WORKDIR /app

# Binary
COPY --from=builder /task-router /app/task-router

# Static assets embedded via filesystem paths (not go:embed)
COPY migrations/ /app/migrations/
COPY tagcatalog/  /app/tagcatalog/

# Create /data as app user so Docker initialises the named volume with app ownership
RUN mkdir /data && chown app:app /data

EXPOSE 8092

USER app

# SQLite data lives on a named volume mounted at /data
VOLUME ["/data"]

ENTRYPOINT ["/app/task-router"]
