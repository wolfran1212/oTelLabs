terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.25"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── Red VPC dedicada ────────────────────────────────────────────────────────
resource "google_compute_network" "otel_vpc" {
  name                    = "otel-lab-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "otel_subnet" {
  name          = "otel-lab-subnet"
  ip_cidr_range = "10.10.0.0/20"
  region        = var.region
  network       = google_compute_network.otel_vpc.id

  secondary_ip_range {
    range_name    = "k8s-pod-range"
    ip_cidr_range = "10.20.0.0/16"
  }

  secondary_ip_range {
    range_name    = "k8s-service-range"
    ip_cidr_range = "10.30.0.0/20"
  }
}

# ── Clúster GKE con Workload Identity ─────────────────────────────────────────
resource "google_container_cluster" "primary" {
  name     = var.cluster_name
  location = var.region

  network    = google_compute_network.otel_vpc.name
  subnetwork = google_compute_subnetwork.otel_subnet.name

  remove_default_node_pool = true
  initial_node_count       = 1

  ip_allocation_policy {
    cluster_secondary_range_name  = "k8s-pod-range"
    services_secondary_range_name = "k8s-service-range"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "DEPLOYMENT"]
    managed_prometheus {
      enabled = true
    }
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  deletion_protection = false
}

# ── Node Pool dedicado ────────────────────────────────────────────────────────
resource "google_container_node_pool" "primary_nodes" {
  name       = "${var.cluster_name}-node-pool"
  location   = var.region
  cluster    = google_container_cluster.primary.name
  node_count = var.node_count

  autoscaling {
    min_node_count = 1
    max_node_count = 5
  }

  node_config {
    machine_type = var.machine_type
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]

    labels = {
      environment = "production"
      app         = "otel-lab"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

# ── Service Account para OTel Collector (Workload Identity) ──────────────────
resource "google_service_account" "otel_collector_sa" {
  account_id   = "otel-collector-sa"
  display_name = "Service Account para OpenTelemetry Collector"
}

# Permisos para Cloud Trace
resource "google_project_iam_member" "trace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.otel_collector_sa.email}"
}

# Permisos para Cloud Logging
resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.otel_collector_sa.email}"
}

# Permisos para Cloud Monitoring (Métricas)
resource "google_project_iam_member" "metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.otel_collector_sa.email}"
}

# Vinculación Workload Identity con el ServiceAccount de Kubernetes
resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = google_service_account.otel_collector_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[otel-lab/otel-collector-sa]"
}
