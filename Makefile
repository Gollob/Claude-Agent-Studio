.PHONY: up down test lint scan build help

help:
	@echo "Targets:"
	@echo "  up     - Start all services (docker compose up -d)"
	@echo "  down   - Stop all services"
	@echo "  build  - Build all Docker images"
	@echo "  test   - Run all tests (pytest + go test)"
	@echo "  lint   - Run linters (ruff + go vet + gofmt check)"
	@echo "  scan   - Run secret scan (gitleaks)"

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

test: test-bot test-router

test-bot:
	@echo "--- pytest (bot) ---"
	cd bot && python -m pytest tests/ -v

test-router:
	@echo "--- go test (task-router) ---"
	cd services/task-router && go test ./...

lint: lint-bot lint-router

lint-bot:
	@echo "--- ruff (bot) ---"
	cd bot && python -m ruff check . || true

lint-router:
	@echo "--- go vet + gofmt (task-router) ---"
	cd services/task-router && go vet ./...
	@echo "gofmt check:"
	@test -z "$$(cd services/task-router && gofmt -l .)" || (echo "gofmt: files need formatting:" && cd services/task-router && gofmt -l . && exit 1)

scan:
	@echo "--- gitleaks secret scan ---"
	@if command -v gitleaks >/dev/null 2>&1; then \
		gitleaks detect --source . --redact --no-banner; \
	else \
		docker run --rm -v "$(PWD):/repo" zricethezav/gitleaks:latest detect --source /repo --redact --no-banner; \
	fi
	@echo "--- grep checklist ---"
	@grep -rInE 'sk-ant-|oat01|TELEGRAM_CHAT_ID=[0-9]' . --exclude-dir=.git --exclude="*.example" && echo "WARNING: potential secrets found" || echo "grep checklist: CLEAN"
