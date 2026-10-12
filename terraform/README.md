# Infraestructura como Código (IaC) — Terraform

Este directorio contiene los módulos de Terraform para aprovisionar la infraestructura requerida para el laboratorio de observabilidad con OpenTelemetry tanto en **GCP** como en **AWS**.

---

## 1. Google Cloud Platform (`terraform/gcp/`)

Aprovisiona:
- Red VPC (`otel-lab-vpc`) y Subred (`otel-lab-subnet`) con rangos secundarios para Pods y Servicios.
- Clúster **GKE** (`otel-lab`) con **Workload Identity** y Google Managed Prometheus habilitados.
- Node Pool dedicado con escalado automático (1 a 5 nodos `e2-standard-4`).
- Google Service Account (`otel-collector-sa`) con roles de **Cloud Trace**, **Cloud Logging** y **Cloud Monitoring**.
- IAM Binding de Workload Identity para el ServiceAccount de Kubernetes en namespace `otel-lab`.

### Uso:
```bash
cd terraform/gcp
cp terraform.tfvars.example terraform.tfvars
# Editar terraform.tfvars con tu project_id
terraform init
terraform plan
terraform apply -auto-approve
```

---

## 2. Amazon Web Services (`terraform/aws/`)

Aprovisiona:
- Red VPC (`10.0.0.0/16`) con subredes públicas en 2 zonas de disponibilidad e Internet Gateway.
- Clúster **AWS ECS Fargate** (`otel-lab`) con Container Insights activo.
- Security Group configurado para tráfico HTTP (8000, 8001) y OTLP gRPC (4317).
- CloudWatch Log Group (`/ecs/otel-lab`).
- IAM Execution Role (`AmazonECSTaskExecutionRolePolicy`).
- IAM Task Role con permisos para **AWS X-Ray** (`AWSXRayDaemonWriteAccess`) y **CloudWatch** (`CloudWatchLogsFullAccess`).

### Uso:
```bash
cd terraform/aws
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply -auto-approve
```
