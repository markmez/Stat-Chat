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

# Bake the database into the image as a fallback seed.
# On startup, ensure_db() copies it to the volume (fast local copy).
# The volume copy is the live DB that gets updated by the cron pipeline.
COPY baseball_stats_full.db /app/seed_db/baseball_stats_full.db

# Copy backend application code
COPY . .

# Database paths — override via env vars in Railway
ENV DB_PATH=/data/baseball_stats_full.db
ENV SEED_DB_PATH=/app/seed_db/baseball_stats_full.db
ENV METERING_DB_PATH=/data/metering.db
ENV FREE_QUERIES_PER_WEEK=5

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
