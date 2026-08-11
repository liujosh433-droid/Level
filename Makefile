.DEFAULT_GOAL := help

PROJECT ?= project-c31bdcdc-f293-47c2-a4c
REGION  ?= us-central1

# Prefer the project-local uv binary (checked into .tools/) so shells without
# a global `uv` on PATH still work. Fall back to PATH.
UV := $(shell if [ -x .tools/uv ]; then echo .tools/uv; else echo uv; fi)

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
# Dev environment
# -----------------------------------------------------------------------------
.PHONY: install
install: ## Install all Python deps via uv workspaces
	$(UV) sync --all-extras --group dev

.PHONY: env
env: ## Bootstrap .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "Created .env — fill in GOOGLE_API_KEY")

.PHONY: smoke
smoke: ## Live Gemini smoke test (needs GOOGLE_API_KEY in .env)
	$(UV) run python scripts/smoke_gemini.py

# -----------------------------------------------------------------------------
# Quality gates
# -----------------------------------------------------------------------------
.PHONY: fmt
fmt: ## Auto-format with ruff
	$(UV) run ruff format packages tests
	$(UV) run ruff check --fix packages tests

.PHONY: lint
lint: ## Lint (ruff + mypy)
	$(UV) run ruff check packages tests
	$(UV) run ruff format --check packages tests
	$(UV) run mypy packages

.PHONY: test
test: ## Run the full test suite
	$(UV) run pytest -x

.PHONY: test-cov
test-cov: ## Tests + coverage report
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=html

.PHONY: check
check: lint test ## Lint + test (what CI runs)

# -----------------------------------------------------------------------------
# Local run
# -----------------------------------------------------------------------------
.PHONY: api
api: ## Run the FastAPI service locally
	$(UV) run uvicorn level_api.main:app --reload --host 0.0.0.0 --port 8080

.PHONY: job-async-challenge
job-async-challenge: ## Run the async challenge Cloud Run Job locally
	$(UV) run python -m level_jobs.async_challenge

.PHONY: job-ingest
job-ingest: ## Run the ingest_all job locally (fixture demo signals)
	LEVEL_INGEST_FIXTURES=1 LEVEL_JOB_USER_IDS=demo-parent $(UV) run python -m level_jobs.ingest_all

.PHONY: job-retain
job-retain: ## Run retention prune (TTL + soft cap)
	LEVEL_JOB_USER_IDS=demo-parent $(UV) run python -m level_jobs.retain

.PHONY: demo-judge
demo-judge: ## Continuous Action proof: ingest → Care Profile → async challenge → retain
	LEVEL_ENV=local LEVEL_INGEST_FIXTURES=1 LEVEL_JOB_USER_IDS=demo-parent \
		$(UV) run python scripts/demo_continuous_action.py

.PHONY: seed
seed: ## Seed demo caregiver narrative into Memory Bank (calls Gemini)
	$(UV) run python scripts/seed_demo_data.py

.PHONY: web
web: ## Run the Next.js web app locally
	cd apps/web && npm run dev

.PHONY: web-install
web-install: ## Install Next.js deps
	cd apps/web && npm install

# -----------------------------------------------------------------------------
# Cloud (require gcloud auth + billing enabled)
# -----------------------------------------------------------------------------
.PHONY: gcloud-auth
gcloud-auth: ## Log in gcloud CLI and application-default creds
	gcloud auth login
	gcloud auth application-default login
	gcloud config set project $(PROJECT)

.PHONY: tf-init
tf-init: ## terraform init
	cd infra/terraform && terraform init

.PHONY: tf-plan
tf-plan: ## terraform plan
	cd infra/terraform && terraform plan -var="project_id=$(PROJECT)" -var="region=$(REGION)"

.PHONY: tf-apply
tf-apply: ## terraform apply (creates all GCP resources)
	cd infra/terraform && terraform apply -var="project_id=$(PROJECT)" -var="region=$(REGION)"

.PHONY: deploy-api
deploy-api: ## Build + deploy the API to Cloud Run
	gcloud builds submit --config=infra/cloudbuild-api.yaml --substitutions=_REGION=$(REGION)

.PHONY: deploy-jobs
deploy-jobs: ## Build + deploy Cloud Run Jobs
	gcloud builds submit --config=infra/cloudbuild-jobs.yaml --substitutions=_REGION=$(REGION)

# -----------------------------------------------------------------------------
# Housekeeping
# -----------------------------------------------------------------------------
.PHONY: clean
clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	rm -rf packages/*/build packages/*/dist packages/*/*.egg-info
