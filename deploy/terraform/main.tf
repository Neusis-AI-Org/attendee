terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # Uncomment and configure for remote state:
  # backend "gcs" {
  #   bucket = "your-tf-state-bucket"
  #   prefix = "attendee"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Enable required APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "sqladmin.googleapis.com",
    "redis.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "servicenetworking.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ── VPC ──────────────────────────────────────────────────────────────────────

resource "google_compute_network" "vpc" {
  name                    = "attendee-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "subnet" {
  name          = "attendee-subnet"
  ip_cidr_range = "10.0.0.0/20"
  region        = var.region
  network       = google_compute_network.vpc.id

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.4.0.0/14"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.8.0.0/20"
  }
}

# ── Cloud NAT (outbound internet for private nodes) ──────────────────────────

resource "google_compute_router" "router" {
  name    = "attendee-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                               = "attendee-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

# Private service access (for Cloud SQL & Memorystore)
resource "google_compute_global_address" "private_ip_range" {
  name          = "attendee-private-ip"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_vpc" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_range.name]
}

# ── GKE Autopilot ───────────────────────────────────────────────────────────

resource "google_container_cluster" "autopilot" {
  name     = var.gke_cluster_name
  location = var.region

  enable_autopilot = true

  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.subnet.id

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  # Allow master to reach nodes for webhooks, exec, logs
  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = "0.0.0.0/0"
      display_name = "All"
    }
  }

  release_channel {
    channel = "REGULAR"
  }

  depends_on = [google_project_service.apis]
}

# ── Cloud SQL (PostgreSQL) ───────────────────────────────────────────────────

resource "random_password" "db_password" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "postgres" {
  name             = "attendee-postgres-${var.environment}"
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier              = var.db_tier
    disk_size         = var.db_disk_size
    disk_autoresize   = true
    availability_type = var.environment == "production" ? "REGIONAL" : "ZONAL"

    ip_configuration {
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.vpc.id
      enable_private_path_for_google_cloud_services = true
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = var.environment == "production"
      start_time                     = "03:00"
    }

    maintenance_window {
      day  = 7 # Sunday
      hour = 4
    }

    database_flags {
      name  = "max_connections"
      value = "200"
    }
  }

  deletion_protection = var.environment == "production"

  depends_on = [google_service_networking_connection.private_vpc]
}

resource "google_sql_database" "attendee" {
  name     = var.db_name
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "attendee" {
  name     = var.db_user
  instance = google_sql_database_instance.postgres.name
  password = random_password.db_password.result
}

# ── Memorystore (Redis) ─────────────────────────────────────────────────────

resource "google_redis_instance" "redis" {
  name           = "attendee-redis-${var.environment}"
  tier           = var.environment == "production" ? "STANDARD_HA" : "BASIC"
  memory_size_gb = var.redis_memory_size_gb
  region         = var.region
  redis_version  = var.redis_version

  authorized_network = google_compute_network.vpc.id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"

  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 4
        minutes = 0
      }
    }
  }

  depends_on = [google_service_networking_connection.private_vpc]
}

# ── GCS Bucket ───────────────────────────────────────────────────────────────

resource "google_storage_bucket" "recordings" {
  name          = var.recording_bucket_name != "" ? var.recording_bucket_name : "attendee-recordings-${var.project_id}"
  location      = var.region
  force_destroy = false

  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
}

# ── Artifact Registry ───────────────────────────────────────────────────────

resource "google_artifact_registry_repository" "attendee" {
  location      = var.region
  repository_id = "attendee"
  format        = "DOCKER"
  description   = "Attendee container images"

  depends_on = [google_project_service.apis]
}

# ── IAM / Service Accounts ──────────────────────────────────────────────────

# App service account (used by web/worker/scheduler pods)
resource "google_service_account" "app" {
  account_id   = "attendee-app"
  display_name = "Attendee App"
}

# Bot pod service account (used by dynamically created bot pods)
resource "google_service_account" "bot" {
  account_id   = "attendee-bot"
  display_name = "Attendee Bot Pods"
}

# App SA needs to create pods (for bot pod creator)
resource "google_project_iam_member" "app_container_developer" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# GCS access for recordings
resource "google_storage_bucket_iam_member" "app_gcs" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
}

resource "google_storage_bucket_iam_member" "bot_gcs" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.bot.email}"
}

# Workload Identity bindings (GKE SA <-> GCP SA)
# These depend on the GKE cluster existing (creates the identity pool)
resource "google_service_account_iam_member" "app_workload_identity" {
  service_account_id = google_service_account.app.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[attendee/attendee-app]"
  depends_on         = [google_container_cluster.autopilot]
}

resource "google_service_account_iam_member" "bot_workload_identity" {
  service_account_id = google_service_account.bot.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[attendee/attendee-bot]"
  depends_on         = [google_container_cluster.autopilot]
}

# Artifact Registry read access for pulling images
resource "google_artifact_registry_repository_iam_member" "app_reader" {
  location   = google_artifact_registry_repository.attendee.location
  repository = google_artifact_registry_repository.attendee.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.app.email}"
}

resource "google_artifact_registry_repository_iam_member" "bot_reader" {
  location   = google_artifact_registry_repository.attendee.location
  repository = google_artifact_registry_repository.attendee.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.bot.email}"
}
