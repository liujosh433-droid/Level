output "api_url" {
  value       = google_cloud_run_v2_service.api.uri
  description = "Public HTTPS URL of the Level API"
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/level"
}

output "api_service_account" {
  value = google_service_account.api.email
}

output "jobs_service_account" {
  value = google_service_account.jobs.email
}
