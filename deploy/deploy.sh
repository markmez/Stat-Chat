#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat deploy script — pull latest code and restart.
#
# Usage (from your Mac):
#   ssh ubuntu@<your-ip> 'sudo bash /opt/statchat/repo/deploy/deploy.sh'
#
# Or just SSH in and run it.
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

echo "=== Deploying StatChat ==="

# Pull latest code
cd /opt/statchat/repo
sudo -u statchat git pull

# Update dependencies if requirements changed
sudo -u statchat /opt/statchat/venv/bin/pip install -q -r backend/requirements.txt

# Restart the service (zero-downtime: systemd handles graceful restart)
sudo systemctl restart statchat

echo "Waiting for service to start..."
sleep 2

# Verify
if sudo systemctl is-active --quiet statchat; then
    echo "✅ StatChat is running"
    curl -s http://127.0.0.1:8000/health | python3 -m json.tool
else
    echo "❌ Service failed to start"
    sudo journalctl -u statchat --no-pager -n 20
    exit 1
fi
