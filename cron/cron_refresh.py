"""Cron job script: triggers a data refresh via the admin endpoint."""
import os
import sys
import requests

BASE_URL = os.getenv("BACKEND_URL", "https://stat-chat-production.up.railway.app")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
HC_PING_URL = "https://hc-ping.com/d3f0c82b-235a-477f-8ed5-3f6ac4c6daa7"

def ping_healthcheck(status=""):
    """Ping Healthchecks.io — empty status = success, '/fail' = failure."""
    try:
        requests.get(f"{HC_PING_URL}{status}", timeout=10)
    except Exception:
        pass  # Don't let monitoring failure break the cron

def refresh():
    try:
        resp = requests.post(
            f"{BASE_URL}/admin/refresh",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=600,
        )
        print(f"Status: {resp.status_code}")
        body = resp.json()
        print(body)
        if resp.status_code != 200:
            ping_healthcheck("/fail")
            sys.exit(1)
        # Backend returns 200 even when the pipeline errors — check body too
        if body.get("status") == "error":
            print("Pipeline reported error — marking as failure.")
            ping_healthcheck("/fail")
            sys.exit(1)
        ping_healthcheck()
    except requests.exceptions.ConnectionError:
        print("WARNING: Backend unreachable — skipping this refresh cycle.")
        ping_healthcheck("/fail")
    except requests.exceptions.Timeout:
        print("WARNING: Refresh timed out (600s limit) — will retry next cycle.")
        ping_healthcheck("/fail")
    except Exception as e:
        print(f"ERROR: {e}")
        ping_healthcheck("/fail")
        sys.exit(1)

if __name__ == "__main__":
    refresh()
