"""Cron job script: triggers a data refresh via the admin endpoint."""
import os
import requests

BASE_URL = os.getenv("BACKEND_URL", "https://stat-chat-production.up.railway.app")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

def refresh():
    resp = requests.post(
        f"{BASE_URL}/admin/refresh",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=300,
    )
    print(f"Status: {resp.status_code}")
    print(resp.json())

if __name__ == "__main__":
    refresh()
