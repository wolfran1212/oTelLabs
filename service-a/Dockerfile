FROM python:3.12.7-slim

WORKDIR /app

# Dependencias del sistema para psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Universal Service Tags (alineados con OTel Resource)
ENV SERVICE_NAME=service-a
ENV APP_VERSION=1.0.0
ENV ENVIRONMENT=production
ENV PROMETHEUS_PORT=9064
#9464/tcp → OpenTelemetry Prometheus exporter segun l a CNCF

# Puerto de la app FastAPI
EXPOSE 8000
# Puerto de métricas Prometheus
EXPOSE 9064

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
#recordar GIL que: solo un hilo ejecute código Python a la vez. Esto significa que un solo proceso Python no puede usar múltiples núcleos de CPU simultáneamente para procesar requests en paralelo por eso hay 2 workers por cada nucleo, 2 requests CPU-bound
#pyhton vs otros:  operaciones costosas son I/O, no CPU y fastapi sobre starlettet usa perfacto el asyncronismo
