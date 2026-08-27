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

variable "demo_in_cloud" {
  description = <<-EOT
    Enable OAuth-less demo mode on the deployed API so judges can click Try
    demo on the hosted landing page. Off by default (the /v1/auth/demo
    endpoint 404s so a probe can't spawn synthetic users). When true, the
    endpoint is gated by a fixed slot pool (see demo_slots_per_scenario) and
    a per-IP rate limit (see demo_per_ip_per_hour). See SETUP.md #hosted-
    demo-in-cloud for the full safety story.
  EOT
  type        = bool
  default     = false
}

variable "demo_slots_per_scenario" {
  description = "Size of the demo user pool per scenario. Total demo users = value * 2 scenarios."
  type        = number
  default     = 3
}

variable "demo_per_ip_per_hour" {
  description = "Per-IP token bucket capacity + refill (per hour) on /v1/auth/demo."
  type        = number
  default     = 10
}
