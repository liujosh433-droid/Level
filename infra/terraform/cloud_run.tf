resource "google_cloud_run_v2_service" "api" {
  name     = "level-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 0
      max_instance_count = 4
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/level/api:latest"

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
        name  = "LEVEL_WEB_APP_URL"
        value = var.web_app_url
      }
      env {
        name  = "LEVEL_PUBLIC_API_URL"
        value = var.public_api_url
      }
      env {
        name  = "LEVEL_OTEL_EXPORTER"
        value = "cloud"
      }
      env {
        name  = "GOOGLE_OAUTH_CLIENT_ID"
        value = var.google_oauth_client_id
      }
      env {
        name = "GOOGLE_OAUTH_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.oauth_client_secret.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "LEVEL_SESSION_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_secret.secret_id
            version = "latest"
          }
        }
      }

      # Hosted demo mode. When demo_in_cloud=true, judges hit
      # /v1/auth/demo directly on the deployed URL and land in a
      # synthetic user's world - see SETUP.md #hosted-demo-in-cloud
      # for the pool + rate-limit + cost-cap safety envelope.
      env {
        name  = "LEVEL_DEMO_IN_CLOUD"
        value = var.demo_in_cloud ? "true" : "false"
      }
      env {
        name  = "LEVEL_DEMO_SLOTS_PER_SCENARIO"
        value = tostring(var.demo_slots_per_scenario)
      }
      env {
        name  = "LEVEL_DEMO_PER_IP_PER_HOUR"
        value = tostring(var.demo_per_ip_per_hour)
      }

      # Demo real-send mode. Master toggle is always emitted (false
      # by default) so the runtime code has a deterministic signal;
      # the intercept address rides along the same way. The refresh
      # token is pulled from Secret Manager and only wired up when
      # the feature is enabled - see the `dynamic` block below for
      # the conditional mount.
      env {
        name  = "LEVEL_DEMO_SEND_REAL_EMAILS"
        value = var.demo_send_real_emails ? "true" : "false"
      }
      env {
        name  = "LEVEL_DEMO_EMAIL_INTERCEPT_TO"
        value = var.demo_email_intercept_to
      }
      dynamic "env" {
        # `for_each` on a list literal is the idiomatic way to make
        # a single-shot conditional env block: length-1 list emits
        # once, empty list emits zero times.
        for_each = var.demo_send_real_emails ? [1] : []
        content {
          name = "LEVEL_DEMO_GMAIL_REFRESH_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.demo_gmail_refresh_token[0].secret_id
              version = "latest"
            }
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_artifact_registry_repository.level,
    google_secret_manager_secret_version.session_secret_v1,
    google_secret_manager_secret_version.oauth_client_secret_v1,
    # No dependency on the optional demo refresh-token secret -
    # it's a count-guarded resource and Terraform can't express a
    # depends_on on a resource that may not exist. The dynamic env
    # block above only references it when the count is 1, so the
    # implicit dependency is picked up correctly there.
  ]
}

resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = google_cloud_run_v2_service.api.project
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
