variable "project_id" {
  description = "GCP project id"
  type        = string
}

variable "region" {
  description = "Default region for Cloud Run + Artifact Registry"
  type        = string
  default     = "us-central1"
}

variable "web_app_url" {
  description = "Public URL of the Next.js app (used for CORS + OAuth redirect)."
  type        = string
}

variable "public_api_url" {
  description = "Public HTTPS URL of the Cloud Run API (used for calendar watch)."
  type        = string
  default     = ""
}

variable "session_secret" {
  description = "Long random string used to sign cookies. Store in Secret Manager and pull in via env."
  type        = string
  sensitive   = true
}

variable "google_oauth_client_id" {
  description = "OAuth client id (Web application)."
  type        = string
}

variable "google_oauth_client_secret" {
  description = "OAuth client secret."
  type        = string
  sensitive   = true
}
