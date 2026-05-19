FROM python:3.12-slim

WORKDIR /app

# Dépendances système minimales
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Installer les dépendances Python (requirements-api.txt = sans mcp)
COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt

# Copier le code (sans data/ ni .env grâce au .dockerignore)
COPY api_server.py .

EXPOSE 8000

# Gunicorn + workers uvicorn pour la prod
CMD ["gunicorn", "api_server:app", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--timeout", "60", "--forwarded-allow-ips", "*"]
