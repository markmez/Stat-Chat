# Build context: project root (not the backend/ subdirectory)
# Build command: docker build -f backend/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy schema from project root (shared with iOS data pipeline)
COPY schema_description.py .

# Copy backend application code
COPY backend/ .

# Database paths — override via env vars in Railway/Fly.io
ENV DB_PATH=/data/baseball_stats.db
ENV METERING_DB_PATH=/data/metering.db
ENV FREE_QUERIES_PER_WEEK=5

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
