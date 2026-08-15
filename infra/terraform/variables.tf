variable "project_id" {
  description = "GCP project ID (e.g. level-hack-2026)."
  type        = string
}

variable "region" {
  description = "Primary GCP region."
  type        = string
  default     = "us-central1"
}

variable "env" {
  description = "Environment tag (dev / staging / prod). Used for naming + labels."
  type        = string
  default     = "dev"
}

variable "enable_vector_search" {
  description = <<-EOT
    Spin up Vertex AI Vector Search (Index + Endpoint). Costs ~$30–90/mo while
    the endpoint is up. Set true as soon as you want real semantic retrieval —
    preferred for early testing. Leave false only for cost-free CI.
  EOT
  type        = bool
  default     = true
}

variable "enable_model_armor" {
  description = "Create Model Armor templates for inbound + outbound guardrails."
  type        = bool
  default     = true
}

variable "apis_to_enable" {
  description = "GCP APIs Level needs. Enabled on apply."
  type        = list(string)
  default = [
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "firestore.googleapis.com",
    "storage.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "logging.googleapis.com",
    "cloudtrace.googleapis.com",
    "modelarmor.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "calendar-json.googleapis.com",
    "gmail.googleapis.com",
  ]
}
