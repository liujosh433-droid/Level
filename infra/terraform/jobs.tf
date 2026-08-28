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
        # Required by calendar.sync.ensure_watch — the nightly job
        # renews Google push channels and skips silently when the
        # public URL isn't HTTPS. Sourced from an output so we don't
        # duplicate the run-service URL in variables.
        env {
          name  = "LEVEL_PUBLIC_API_URL"
          value = google_cloud_run_v2_service.api.uri
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_artifact_registry_repository.level,
  ]
}

# Cloud Scheduler needs run.invoker on the target Job to actually
# execute it. Without this the scheduler POST returns 403 every
# night and the trigger silently no-ops. Grants the dedicated
# scheduler SA (see main.tf) rather than reusing the job's own SA.
resource "google_cloud_run_v2_job_iam_member" "scheduler_invokes_nightly" {
  project  = var.project_id
  location = google_cloud_run_v2_job.nightly.location
  name     = google_cloud_run_v2_job.nightly.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
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
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_cloud_run_v2_job_iam_member.scheduler_invokes_nightly,
  ]
}
