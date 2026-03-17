# Build context: backend/ (Railway root directory)
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy schema (uploaded separately to backend/ for Docker context)
COPY schema_description.py .

# Copy data pipeline scripts (for live data refresh + streak detection)
COPY data_pipeline/ data_pipeline/

# Copy backend application code
COPY . .

# Move DB to seed dir if present (local deploy via railway up).
# When deploying via GitHub (no DB in repo), seed dir stays empty —
# the service uses the existing volume DB instead.
RUN mkdir -p /app/seed_db && \
    if [ -f /app/baseball_stats_full.db ]; then \
        mv /app/baseball_stats_full.db /app/seed_db/; \
    fi

# Database paths — override via env vars in Railway
ENV DB_PATH=/data/baseball_stats_full.db
ENV SEED_DB_PATH=/app/seed_db/baseball_stats_full.db
ENV METERING_DB_PATH=/data/metering.db
ENV FREE_QUERIES_PER_WEEK=5

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
