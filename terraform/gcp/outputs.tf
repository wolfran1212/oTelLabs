output "cluster_name" {
  description = "Nombre del clúster GKE creado"
  value       = google_container_cluster.primary.name
}

output "cluster_endpoint" {
  description = "Endpoint del API server de GKE"
  value       = google_container_cluster.primary.endpoint
}

output "get_credentials_command" {
  description = "Comando para autenticar kubectl con el clúster"
  value       = "gcloud container clusters get-credentials ${google_container_cluster.primary.name} --region=${var.region} --project=${var.project_id}"
}

output "collector_service_account_email" {
  description = "Email del Service Account de GCP para el OTel Collector"
  value       = google_service_account.otel_collector_sa.email
}
