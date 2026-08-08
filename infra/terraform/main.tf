locals {
  labels = {
    app         = "level"
    environment = var.env
    hackathon   = "all-things-agentic-2026"
  }

  # Per-agent service accounts (Agent Identity — Fortified Enterprise Fleet).
  # Principle of least privilege: each agent SA gets only the roles it needs.
  agent_accounts = {
    framer            = { roles = ["roles/aiplatform.user", "roles/logging.logWriter"] }
    retriever         = { roles = ["roles/aiplatform.user", "roles/datastore.user", "roles/logging.logWriter"] }
    challenger        = { roles = ["roles/aiplatform.user", "roles/logging.logWriter"] }
    judge             = { roles = ["roles/aiplatform.user", "roles/datastore.user", "roles/logging.logWriter"] }
    ingest_normalizer = { roles = ["roles/aiplatform.user", "roles/datastore.user", "roles/storage.objectViewer", "roles/logging.logWriter"] }
    conductor         = { roles = ["roles/aiplatform.user", "roles/datastore.user", "roles/logging.logWriter", "roles/cloudtrace.agent"] }
    api               = { roles = ["roles/run.invoker", "roles/datastore.user", "roles/secretmanager.secretAccessor", "roles/logging.logWriter", "roles/cloudtrace.agent"] }
    jobs              = { roles = ["roles/aiplatform.user", "roles/datastore.user", "roles/storage.objectAdmin", "roles/logging.logWriter"] }
  }
}

# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "apis" {
  for_each                   = toset(var.apis_to_enable)
  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}

# ---------------------------------------------------------------------------
# Firestore (Memory Bank — structured state)
# ---------------------------------------------------------------------------

resource "google_firestore_database" "default" {
  project                           = var.project_id
  name                              = "(default)"
  location_id                       = var.region
  type                              = "FIRESTORE_NATIVE"
  concurrency_mode                  = "OPTIMISTIC"
  app_engine_integration_mode       = "DISABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"
  delete_protection_state           = "DELETE_PROTECTION_DISABLED" # hackathon: allow teardown

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Cloud Storage (raw signals: voice, screenshots, chat exports)
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "signals" {
  name                        = "${var.project_id}-level-signals"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true # hackathon: allow teardown
  labels                      = local.labels

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Agent Identity — one service account per agent + runtime
# ---------------------------------------------------------------------------

resource "google_service_account" "agents" {
  for_each     = local.agent_accounts
  account_id   = "level-${replace(each.key, "_", "-")}"
  display_name = "Level agent: ${each.key}"
  description  = "Least-privilege identity for the Level ${each.key} agent/runtime."
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "agent_roles" {
  for_each = {
    for pair in flatten([
      for name, cfg in local.agent_accounts : [
        for role in cfg.roles : {
          key  = "${name}:${role}"
          name = name
          role = role
        }
      ]
    ]) : pair.key => pair
  }

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agents[each.value.name].email}"
}
