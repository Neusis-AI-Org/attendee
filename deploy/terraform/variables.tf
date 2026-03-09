variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name (e.g. production, staging)"
  type        = string
  default     = "production"
}

variable "domain" {
  description = "Optional domain name for the application (e.g. app.neusis.com)"
  type        = string
  default     = ""
}

# ── GKE ──────────────────────────────────────────────────────────────────────

variable "gke_cluster_name" {
  description = "GKE Autopilot cluster name"
  type        = string
  default     = "attendee"
}

# ── Cloud SQL ────────────────────────────────────────────────────────────────

variable "db_tier" {
  description = "Cloud SQL machine tier"
  type        = string
  default     = "db-custom-2-8192" # 2 vCPU, 8 GB RAM
}

variable "db_disk_size" {
  description = "Cloud SQL disk size in GB"
  type        = number
  default     = 20
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "attendee"
}

variable "db_user" {
  description = "PostgreSQL user name"
  type        = string
  default     = "attendee"
}

# ── Memorystore (Redis) ─────────────────────────────────────────────────────

variable "redis_memory_size_gb" {
  description = "Memorystore Redis memory in GB"
  type        = number
  default     = 2
}

variable "redis_version" {
  description = "Redis version"
  type        = string
  default     = "REDIS_7_0"
}

# ── GCS ──────────────────────────────────────────────────────────────────────

variable "recording_bucket_name" {
  description = "GCS bucket name for recordings"
  type        = string
  default     = ""
}
