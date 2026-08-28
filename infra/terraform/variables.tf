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

variable "demo_send_real_emails" {
  description = <<-EOT
    Flip the /v1/email/send demo short-circuit from "preview" to a real Gmail
    send using the operator's own refresh token, with the recipient rewritten
    unconditionally to demo_email_intercept_to. Used to produce actual email
    proof in a demo recording. Only fires for demo users. Off by default; when
    true, demo_gmail_refresh_token MUST also be set or the runtime falls back
    to preview mode. See SETUP.md #demo-real-send-mode.
  EOT
  type        = bool
  default     = false
}

variable "demo_email_intercept_to" {
  description = <<-EOT
    Recipient every demo send is rewritten to when demo_send_real_emails=true.
    Point at your own inbox so mail can never leak to a fake demo contact.
  EOT
  type        = string
  default     = ""
}

variable "demo_gmail_refresh_token" {
  description = <<-EOT
    Refresh token for the Gmail account that will actually send demo emails.
    Marked sensitive; stored in Secret Manager. Only mounted into Cloud Run
    when demo_send_real_emails=true. Get one by running the normal OAuth flow
    against your own Google account locally and pulling refresh_token out of
    .level/local_store/<uid>/tokens.json.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}
