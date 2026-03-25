#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat Lightsail Setup Script
#
# Run this ONCE on a fresh Ubuntu 22.04 Lightsail instance.
# Prerequisites: SSH access, ports 22/80/443 open, static IP assigned.
#
# Usage: ssh ubuntu@<your-ip> 'bash -s' < setup.sh
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

echo "=== StatChat Lightsail Setup ==="

# ── 1. System packages ──────────────────────────────────────────────
echo "Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip \
    nginx certbot python3-certbot-nginx git sqlite3 awscli

# ── 2. Create app user ──────────────────────────────────────────────
echo "Creating statchat user..."
if ! id statchat &>/dev/null; then
    sudo useradd -m -s /bin/bash statchat
fi

# ── 3. App directory ────────────────────────────────────────────────
echo "Setting up app directory..."
sudo mkdir -p /opt/statchat
sudo mkdir -p /data
sudo chown statchat:statchat /opt/statchat /data

# ── 4. Clone repo ───────────────────────────────────────────────────
echo "Cloning repository..."
sudo -u statchat git clone https://github.com/markmez/Stat-Chat.git /opt/statchat/repo || {
    echo "Repo already exists, pulling latest..."
    sudo -u statchat git -C /opt/statchat/repo pull
}

# ── 5. Python virtual environment ───────────────────────────────────
echo "Setting up Python venv..."
sudo -u statchat python3.11 -m venv /opt/statchat/venv
sudo -u statchat /opt/statchat/venv/bin/pip install --upgrade pip
sudo -u statchat /opt/statchat/venv/bin/pip install -r /opt/statchat/repo/backend/requirements.txt

# ── 6. Download DB from S3 ──────────────────────────────────────────
echo "Downloading database from S3..."
if [ ! -f /data/baseball_stats_full.db ]; then
    sudo -u statchat aws s3 cp s3://stat-chat/baseball_stats_full.db /data/baseball_stats_full.db
    echo "Database downloaded ($(du -h /data/baseball_stats_full.db | cut -f1))"
else
    echo "Database already exists ($(du -h /data/baseball_stats_full.db | cut -f1))"
fi

# ── 7. Environment file ─────────────────────────────────────────────
echo "Creating env file template..."
if [ ! -f /opt/statchat/.env ]; then
    cat > /tmp/statchat-env <<'ENVEOF'
ANTHROPIC_API_KEY=your-key-here
MSF_API_KEY=your-key-here
ADMIN_KEY=I9-NNJ-GBen3SZ-wf8JkZX5-_zvvt8Qri2EtTxWUo-I
DB_PATH=/data/baseball_stats_full.db
METERING_DB_PATH=/data/metering.db
FREE_QUERIES_PER_WEEK=5
ENVEOF
    sudo mv /tmp/statchat-env /opt/statchat/.env
    sudo chown statchat:statchat /opt/statchat/.env
    sudo chmod 600 /opt/statchat/.env
    echo "⚠️  IMPORTANT: Edit /opt/statchat/.env with your real API keys!"
else
    echo "Env file already exists, skipping."
fi

# ── 8. systemd service ──────────────────────────────────────────────
echo "Installing systemd service..."
sudo cp /opt/statchat/repo/deploy/statchat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable statchat

# ── 9. nginx config ─────────────────────────────────────────────────
echo "Installing nginx config..."
sudo cp /opt/statchat/repo/deploy/statchat-nginx.conf /etc/nginx/sites-available/statchat
sudo ln -sf /etc/nginx/sites-available/statchat /etc/nginx/sites-enabled/statchat
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# ── 10. Cron for data refresh ───────────────────────────────────────
echo "Installing cron jobs..."
sudo cp /opt/statchat/repo/deploy/statchat-cron /etc/cron.d/statchat
sudo chmod 644 /etc/cron.d/statchat

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit API keys:  sudo nano /opt/statchat/.env"
echo "  2. Start service:  sudo systemctl start statchat"
echo "  3. Check status:   sudo systemctl status statchat"
echo "  4. View logs:      sudo journalctl -u statchat -f"
echo "  5. SSL (after DNS): sudo certbot --nginx -d api.statchat.com"
echo ""
