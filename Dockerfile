FROM python:3.11-slim

# Install only what we need – no dev tools, no cache
RUN pip install --no-cache-dir --upgrade pip

WORKDIR /app

# Install dependencies first so Docker layer-caches them separately from source
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY *.py ./

# /data is mounted as a volume and holds emails.db + actions.log
RUN mkdir -p /data

# Environment defaults – override via docker-compose or -e flags
ENV DB_PATH=/data/emails.db
ENV LOG_PATH=/data/actions.log

# No default CMD – all subcommands are passed explicitly via docker compose run
ENTRYPOINT ["python", "main.py"]
