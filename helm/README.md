# Charts de Helm — Despliegue en Kubernetes / GKE

Este directorio contiene los Helm charts modulares para desplegar la solución completa de observabilidad en Kubernetes.

---

## 1. OTel Collector (`helm/otel-collector/`)

Despliega el OpenTelemetry Collector como un `DaemonSet` (un pod por nodo de Kubernetes) o `Deployment` con el pipeline completo:
- **Receivers:** OTLP (gRPC :4317, HTTP :4318), Prometheus scraping (:8888).
- **Processors:** `memory_limiter`, `resource`, `filter/health`, `batch`.
- **Exporters:** `googlecloud` (Cloud Trace, Cloud Logging), `prometheus` (:8889), `logging`.
- **ServiceAccount:** Anotado con la Google Service Account vía Workload Identity.

### Instalación:
```bash
helm upgrade --install otel-collector ./helm/otel-collector \
  --namespace otel-lab \
  --create-namespace \
  --set environment.GCP_PROJECT_ID="tu-gcp-project-id"
```

---

## 2. Microservicios (`helm/microservices/`)

Despliega la aplicación distribuida:
- **PostgreSQL:** Base de datos relacional con inicialización automática de esquema y tablas `orders` e `inventory`.
- **Service A:** Microservicio orquestador (FastAPI + OTel SDK) con servicio tipo `LoadBalancer`.
- **Service B:** Microservicio de inventario (FastAPI + OTel SDK) con servicio tipo `ClusterIP`.
- **ConfigMap:** Variables de entorno y cadenas de conexión centralizadas.

### Instalación:
```bash
helm upgrade --install microservices ./helm/microservices \
  --namespace otel-lab \
  --set serviceA.image.repository="gcr.io/tu-gcp-project-id/service-a" \
  --set serviceB.image.repository="gcr.io/tu-gcp-project-id/service-b"
```

### Verificación:
```bash
kubectl get pods -n otel-lab
kubectl get svc -n otel-lab
```
