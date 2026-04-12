.PHONY: help dev dev-down test test-unit test-integration test-smoke test-e2e test-all lint format typecheck frontend backend clean

help:
	@echo "Agentloom dev targets:"
	@echo "  make dev            - docker compose up (postgres + redis + backend)"
	@echo "  make dev-down       - docker compose down"
	@echo "  make backend        - run backend locally (no docker)"
	@echo "  make frontend       - run frontend dev server"
	@echo "  make test           - unit + integration backend tests"
	@echo "  make test-unit      - backend unit tests only"
	@echo "  make test-smoke     - live API smoke tests (needs env keys)"
	@echo "  make test-e2e       - frontend playwright tests"
	@echo "  make lint           - ruff + tsc checks"
	@echo "  make format         - ruff format + prettier"

dev:
	docker compose up -d postgres redis
	cd backend && uvicorn agentloom.main:app --reload --host 0.0.0.0 --port 8000

dev-down:
	docker compose down

backend:
	cd backend && uvicorn agentloom.main:app --reload --host 0.0.0.0 --port 8000

frontend:
	cd frontend && npm run dev

test:
	cd backend && pytest ../tests/backend/unit ../tests/backend/integration -q

test-unit:
	cd backend && pytest ../tests/backend/unit -q

test-integration:
	cd backend && pytest ../tests/backend/integration -q

test-smoke:
	cd backend && pytest ../tests/backend/smoke -q -v

test-e2e:
	cd frontend && npm run test:e2e

test-all: test test-smoke test-e2e

lint:
	cd backend && ruff check agentloom && mypy agentloom || true
	cd frontend && npm run lint || true

format:
	cd backend && ruff format agentloom
	cd frontend && npm run format || true

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf backend/data/* frontend/dist 2>/dev/null || true
