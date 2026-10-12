variable "project_id" {
  description = "El ID del proyecto de Google Cloud"
  type        = string
  default     = "banded-water-506101-p8"
}

variable "region" {
  description = "Región de GCP para el clúster GKE"
  type        = string
  default     = "us-central1"
}

variable "cluster_name" {
  description = "Nombre del clúster GKE"
  type        = string
  default     = "otel-lab"
}

variable "node_count" {
  description = "Número inicial de nodos por zona en el Node Pool"
  type        = number
  default     = 1
}

variable "machine_type" {
  description = "Tipo de máquina para los nodos de GKE"
  type        = string
  default     = "e2-standard-4"
}
