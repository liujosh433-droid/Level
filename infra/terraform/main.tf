locals {
  services = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "firestore.googleapis.com",
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "calendar-json.googleapis.com",
    "gmail.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each                   = toset(local.services)
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

resource "google_firestore_database" "default" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.enabled]
}

resource "google_artifact_registry_repository" "level" {
  location      = var.region
  repository_id = "level"
  description   = "Level API + Jobs images"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled]
}

resource "google_service_account" "api" {
  account_id   = "level-api"
  display_name = "Level FastAPI runtime"
}

resource "google_service_account" "jobs" {
  account_id   = "level-nightly"
  display_name = "Level nightly job runtime"
}

resource "google_project_iam_member" "api_iam" {
  for_each = toset([
    "roles/datastore.user",
    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/secretmanager.secretAccessor",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "jobs_iam" {
  for_each = toset([
    "roles/datastore.user",
    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/secretmanager.secretAccessor",
  ])
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.jobs.email}"
}

resource "google_secret_manager_secret" "session_secret" {
  secret_id = "level-session-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "session_secret_v1" {
  secret      = google_secret_manager_secret.session_secret.id
  secret_data = var.session_secret
}

resource "google_secret_manager_secret" "oauth_client_secret" {
  secret_id = "level-google-oauth-client-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "oauth_client_secret_v1" {
  secret      = google_secret_manager_secret.oauth_client_secret.id
  secret_data = var.google_oauth_client_secret
}

# Refresh token for the Gmail account that sends demo emails.
# Only provisioned when demo_send_real_emails=true so an operator
# who doesn't need this feature never has a stale credential
# sitting in Secret Manager. Both the secret and its first version
# are conditional; ``count = ? 1 : 0`` is the idiomatic pattern for
# an optional Terraform resource in a single-tenant module.
resource "google_secret_manager_secret" "demo_gmail_refresh_token" {
  count     = var.demo_send_real_emails ? 1 : 0
  secret_id = "level-demo-gmail-refresh-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "demo_gmail_refresh_token_v1" {
  count       = var.demo_send_real_emails ? 1 : 0
  secret      = google_secret_manager_secret.demo_gmail_refresh_token[0].id
  secret_data = var.demo_gmail_refresh_token
}
