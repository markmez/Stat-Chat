"""Cron job script: triggers a data refresh via the admin endpoint."""
import os
import sys
import requests

BASE_URL = os.getenv("BACKEND_URL", "https://api.secondsignalapps.com")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

def refresh():
    try:
        resp = requests.post(
            f"{BASE_URL}/admin/refresh",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=600,
        )
        print(f"Status: {resp.status_code}")
        print(resp.json())
        if resp.status_code != 200:
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("WARNING: Backend unreachable — skipping this refresh cycle.")
    except requests.exceptions.Timeout:
        print("WARNING: Refresh timed out (600s limit) — will retry next cycle.")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    refresh()
