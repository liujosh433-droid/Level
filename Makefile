.PHONY: help install dev api web test test-unit test-security test-e2e test-e2e-web \
        lint format tf-init tf-plan tf-apply deploy-api deploy-jobs \
        demo-seed demo-reset diagram clean

UV ?= .tools/uv
PY_DIRS := packages tests

help:
	@echo "Level - caregiver partner (v2)"
	@echo ""
	@echo "  make install         install python + node deps"
	@echo "  make dev             run api :8080 and web :3000 together"
	@echo "  make api             run FastAPI only"
	@echo "  make web             run Next.js only"
	@echo "  make test            unit + security + e2e (fast, LEVEL_ENV=local)"
	@echo "  make test-e2e-web    Playwright smoke against local dev servers"
	@echo "  make lint            ruff + mypy"
	@echo "  make format          ruff format"
	@echo "  make diagram         render docs/architecture.png from .mmd"
	@echo "  make demo-seed       populate .level with Alpha/Beta demo data"
	@echo "  make demo-reset      wipe demo user + ai_audit for fresh recording"
	@echo "  make tf-init/plan/apply    terraform in infra/terraform"
	@echo "  make deploy-api      build + deploy FastAPI to Cloud Run"
	@echo "  make deploy-jobs     build + deploy nightly Cloud Run Job"

install:
	$(UV) sync --all-packages --group dev
	cd apps/web && npm install

dev:
	@echo "Starting api :8080 and web :3000 ..."
	@$(MAKE) -j 2 api web

api:
	LEVEL_ENV=local $(UV) run --package level-api uvicorn level_api.main:app --host 127.0.0.1 --port 8080 --reload

web:
	cd apps/web && npm run dev

test: test-unit test-security test-e2e

test-unit:
	LEVEL_ENV=local $(UV) run --package level-api pytest tests/unit

test-security:
	LEVEL_ENV=local $(UV) run --package level-api pytest tests/security

test-e2e:
	LEVEL_ENV=local $(UV) run --package level-api pytest tests/e2e

test-e2e-web:
	cd apps/web && npx playwright test

lint:
	$(UV) run ruff check $(PY_DIRS)
	$(UV) run mypy

format:
	$(UV) run ruff format $(PY_DIRS)
	$(UV) run ruff check --fix $(PY_DIRS)

diagram:
	@which mmdc >/dev/null || npm i -g @mermaid-js/mermaid-cli
	mmdc -i docs/architecture.mmd -o docs/architecture.png -b transparent

demo-seed:
	LEVEL_ENV=local $(UV) run --package level-jobs python -m level_jobs.demo_seed

demo-reset:
	rm -rf .level/local_store/demo-user
	@echo "Demo user wiped."

tf-init:
	cd infra/terraform && terraform init

tf-plan:
	cd infra/terraform && terraform plan

tf-apply:
	cd infra/terraform && terraform apply

deploy-api:
	gcloud builds submit --tag $$GOOGLE_CLOUD_REGION-docker.pkg.dev/$$GOOGLE_CLOUD_PROJECT/level/api:latest packages/api
	gcloud run deploy level-api --image $$GOOGLE_CLOUD_REGION-docker.pkg.dev/$$GOOGLE_CLOUD_PROJECT/level/api:latest --region $$GOOGLE_CLOUD_REGION --allow-unauthenticated

deploy-jobs:
	gcloud builds submit --tag $$GOOGLE_CLOUD_REGION-docker.pkg.dev/$$GOOGLE_CLOUD_PROJECT/level/jobs:latest packages/jobs
	gcloud run jobs deploy level-nightly --image $$GOOGLE_CLOUD_REGION-docker.pkg.dev/$$GOOGLE_CLOUD_PROJECT/level/jobs:latest --region $$GOOGLE_CLOUD_REGION

clean:
	rm -rf .venv apps/web/node_modules apps/web/.next
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
