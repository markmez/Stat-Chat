FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY cron_refresh.py .
CMD ["python3", "cron_refresh.py"]
