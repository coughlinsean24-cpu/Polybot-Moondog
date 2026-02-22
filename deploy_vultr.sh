#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# Polybot Snipez — Vultr Ubuntu 22.04 Deploy Script
# Run this ON the VPS after uploading the bot files.
#
# Usage:
#   1. SSH into your Vultr Amsterdam VPS
#   2. Upload bot files to /root/polybot/ (see DEPLOY_GUIDE.md)
#   3. Run:  bash deploy_vultr.sh
# ══════════════════════════════════════════════════════════════════════════════

set -e  # Exit on any error

BOT_DIR="/root/polybot"
PYTHON_VERSION="3.11"

echo ""
echo "  ╔════════════════════════════════════════════╗"
echo "  ║   POLYBOT SNIPEZ — VULTR DEPLOY SCRIPT    ║"
echo "  ╚════════════════════════════════════════════╝"
echo ""

# ── 1. System packages ──────────────────────────────────────────────────────
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev \
    python3-pip ufw curl git > /dev/null 2>&1
echo "  ✓ Python ${PYTHON_VERSION} installed"

# ── 2. Firewall ─────────────────────────────────────────────────────────────
echo "[2/6] Configuring firewall..."
ufw allow OpenSSH > /dev/null 2>&1
ufw allow 5050/tcp > /dev/null 2>&1   # Dashboard port
ufw --force enable > /dev/null 2>&1
echo "  ✓ Firewall: SSH + port 5050 open"

# ── 3. Python venv + dependencies ───────────────────────────────────────────
echo "[3/6] Setting up Python virtual environment..."
cd "$BOT_DIR"
python${PYTHON_VERSION} -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✓ Dependencies installed in venv"

# ── 4. Set dashboard password if not already set ────────────────────────────
echo "[4/6] Checking dashboard password..."
if grep -q "^DASHBOARD_PASSWORD=$" .env 2>/dev/null || ! grep -q "DASHBOARD_PASSWORD" .env 2>/dev/null; then
    # Generate a random 16-char password
    PASS=$(openssl rand -base64 12 | tr -d '/+=' | head -c 16)
    if grep -q "DASHBOARD_PASSWORD" .env 2>/dev/null; then
        sed -i "s/^DASHBOARD_PASSWORD=.*/DASHBOARD_PASSWORD=${PASS}/" .env
    else
        echo "" >> .env
        echo "# Dashboard password (set this when running on a VPS!)" >> .env
        echo "DASHBOARD_PASSWORD=${PASS}" >> .env
    fi
    echo "  ✓ Dashboard password set: ${PASS}"
    echo "  ⚠ SAVE THIS PASSWORD — you need it to access the dashboard"
else
    echo "  ✓ Dashboard password already configured"
fi

# ── 5. Create systemd service ───────────────────────────────────────────────
echo "[5/6] Creating systemd service..."
cat > /etc/systemd/system/polybot.service << 'EOF'
[Unit]
Description=Polybot Snipez Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/polybot
ExecStart=/root/polybot/venv/bin/python web_dashboard.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

# Logging
StandardOutput=append:/root/polybot/logs/systemd.log
StandardError=append:/root/polybot/logs/systemd.log

[Install]
WantedBy=multi-user.target
EOF

# Ensure logs directory exists
mkdir -p "$BOT_DIR/logs"

systemctl daemon-reload
systemctl enable polybot.service
echo "  ✓ systemd service created and enabled"

# ── 6. Start the bot ────────────────────────────────────────────────────────
echo "[6/6] Starting Polybot Snipez..."
systemctl start polybot.service
sleep 2

if systemctl is-active --quiet polybot.service; then
    # Get public IP
    PUB_IP=$(curl -s ifconfig.me 2>/dev/null || echo "<YOUR_VPS_IP>")
    echo ""
    echo "  ╔════════════════════════════════════════════╗"
    echo "  ║          DEPLOY COMPLETE ✓                 ║"
    echo "  ╠════════════════════════════════════════════╣"
    echo "  ║  Dashboard: http://${PUB_IP}:5050          "
    echo "  ║  Status:    systemctl status polybot       "
    echo "  ║  Logs:      journalctl -u polybot -f       "
    echo "  ║  Stop:      systemctl stop polybot         "
    echo "  ║  Start:     systemctl start polybot        "
    echo "  ║  Restart:   systemctl restart polybot      "
    echo "  ╚════════════════════════════════════════════╝"
    echo ""
else
    echo ""
    echo "  ⚠ Bot failed to start. Check logs:"
    echo "    journalctl -u polybot -n 50"
    echo ""
fi
