output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}

output "firestore_database" {
  value = google_firestore_database.default.name
}

output "signals_bucket" {
  value = google_storage_bucket.signals.name
}

output "api_url" {
  description = "Cloud Run URL for the Level API. Put this in the demo video."
  value       = google_cloud_run_v2_service.api.uri
}

output "agent_service_accounts" {
  description = "Per-agent service account emails (Agent Identity)."
  value = {
    for name, sa in google_service_account.agents : name => sa.email
  }
}

output "vector_index_id" {
  description = "Set as LEVEL_VECTOR_INDEX_ID."
  value       = var.enable_vector_search ? google_vertex_ai_index.signals[0].name : null
}

output "vector_index_endpoint_id" {
  description = "Set as LEVEL_VECTOR_INDEX_ENDPOINT_ID."
  value       = var.enable_vector_search ? google_vertex_ai_index_endpoint.signals[0].name : null
}

output "vector_deployed_index_id" {
  description = "Set as LEVEL_VECTOR_DEPLOYED_INDEX_ID."
  value       = var.enable_vector_search ? "level_signals_deployed" : null
}

output "model_armor_inbound_template" {
  description = "Set as LEVEL_MODEL_ARMOR_TEMPLATE_INBOUND."
  value = var.enable_model_armor ? (
    "projects/${var.project_id}/locations/${var.region}/templates/level-inbound"
  ) : null
}

output "model_armor_outbound_template" {
  description = "Set as LEVEL_MODEL_ARMOR_TEMPLATE_OUTBOUND."
  value = var.enable_model_armor ? (
    "projects/${var.project_id}/locations/${var.region}/templates/level-outbound"
  ) : null
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.level.repository_id}"
}
