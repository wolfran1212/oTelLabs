variable "aws_region" {
  description = "Región de AWS para desplegar ECS"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Entorno de despliegue"
  type        = string
  default     = "production"
}

variable "cluster_name" {
  description = "Nombre del clúster ECS Fargate"
  type        = string
  default     = "otel-lab"
}

variable "app_cpu" {
  description = "CPU total para la Task Definition Fargate (1024 = 1 vCPU)"
  type        = string
  default     = "1024"
}

variable "app_memory" {
  description = "Memoria total para la Task Definition Fargate (2048 = 2 GB)"
  type        = string
  default     = "2048"
}
