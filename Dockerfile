# Root-level Dockerfile for Railway deployment.
# Build context is the project root (Railway default).
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# schema_description.py lives at repo root (shared with iOS data pipeline)
COPY schema_description.py .

# Backend application code
COPY backend/ .

ENV DB_PATH=/data/baseball_stats.db
ENV METERING_DB_PATH=/data/metering.db
ENV FREE_QUERIES_PER_WEEK=5

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
