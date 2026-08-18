resource "google_cloud_run_v2_job" "nightly" {
  name     = "level-nightly"
  location = var.region

  template {
    template {
      service_account = google_service_account.jobs.email
      max_retries     = 1
      timeout         = "1200s"

      containers {
        image = "${var.region}-docker.pkg.dev/${var.project_id}/level/jobs:latest"

        env {
          name  = "LEVEL_ENV"
          value = "cloud"
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "GOOGLE_CLOUD_REGION"
          value = var.region
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_artifact_registry_repository.level,
  ]
}

resource "google_cloud_scheduler_job" "nightly" {
  name        = "level-nightly-trigger"
  description = "Nightly Level maintenance"
  schedule    = "0 3 * * *"
  time_zone   = "America/Los_Angeles"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.nightly.name}:run"
    oauth_token {
      service_account_email = google_service_account.jobs.email
    }
  }

  depends_on = [google_project_service.enabled]
}
