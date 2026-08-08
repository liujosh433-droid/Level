# ---------------------------------------------------------------------------
# Artifact Registry (container images for api / jobs / web)
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "level" {
  project       = var.project_id
  location      = var.region
  repository_id = "level"
  description   = "Container images for Level API, jobs, and web."
  format        = "DOCKER"
  labels        = local.labels

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Cloud Run — API service (placeholder image; real deploy via Cloud Build)
# ---------------------------------------------------------------------------
# We declare the service skeleton so IAM + URL exist early. The first real
# image is pushed by `make deploy` / Cloud Build. Until then we point at a
# public hello image so the service can be created without a local build.

resource "google_cloud_run_v2_service" "api" {
  name     = "level-api"
  location = var.region
  project  = var.project_id
  labels   = local.labels
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.agents["api"].email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      # Replaced on first `make deploy`. Placeholder keeps terraform apply clean.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

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
      env {
        name  = "LEVEL_OTEL_EXPORTER"
        value = "gcp"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      ports {
        container_port = 8080
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository.level,
    google_service_account.agents,
  ]

  lifecycle {
    ignore_changes = [
      # Image + env are mutated by Cloud Build / deploy.sh after first apply.
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Cloud Run Job — async challenge / ingest workers
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "async_challenge" {
  name     = "level-async-challenge"
  location = var.region
  project  = var.project_id
  labels   = local.labels

  template {
    template {
      service_account = google_service_account.agents["jobs"].email
      timeout         = "900s"
      max_retries     = 1

      containers {
        image = "us-docker.pkg.dev/cloudrun/container/hello"

        env {
          name  = "LEVEL_ENV"
          value = "cloud"
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "1Gi"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_service_account.agents,
  ]

  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}
