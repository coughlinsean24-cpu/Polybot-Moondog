# Polybot Snipez — Vultr Amsterdam Deployment Guide

## Why Amsterdam?
Polymarket's CLOB is a standard limit order book with **Price-Time (FIFO)** priority.  
At the same price level ($0.02), the order placed **first** gets filled first.  
Amsterdam reduces network latency to Polymarket's infrastructure → your orders get queued earlier → more fills.

---

## Step 1: Create Vultr VPS

1. Go to [vultr.com](https://vultr.com) → Deploy New Server
2. **Location:** Amsterdam (NL)
3. **OS:** Ubuntu 22.04 LTS
4. **Plan:** Regular Cloud Compute — $6/mo (1 vCPU, 1GB RAM) is plenty
5. **Hostname:** `polybot` (or whatever)
6. Click **Deploy Now**
7. Wait for it to spin up, note the **IP address** and **root password**

---

## Step 2: Upload Bot Files

From your local Windows machine, open PowerShell:

```powershell
# Install scp if needed (comes with OpenSSH, built into Windows 10+)
# Replace <VPS_IP> with your Vultr server's IP

# Create directory on VPS
ssh root@<VPS_IP> "mkdir -p /root/polybot"

# Upload all bot files
scp -r "C:\Users\Owner\OneDrive\Desktop\Polybot Snipez\*" root@<VPS_IP>:/root/polybot/
```

**Files to upload:**
- `web_dashboard.py`
- `polymarket_client.py`
- `ws_feed.py`
- `config.py`
- `logger.py`
- `data_recorder.py`
- `requirements.txt`
- `.env`
- `deploy_vultr.sh`

> ⚠️ Make sure `.env` is uploaded — it has your API keys!

---

## Step 3: Run Deploy Script

```bash
ssh root@<VPS_IP>
cd /root/polybot
bash deploy_vultr.sh
```

The script will:
- Install Python 3.11 + dependencies
- Open firewall for SSH + port 5050
- Auto-generate a dashboard password (if not set)
- Create a systemd service that auto-restarts on crash
- Start the bot

---

## Step 4: Access Dashboard

Open your browser:
```
http://<VPS_IP>:5050
```
Enter the dashboard password shown during deploy.

---

## Useful Commands

| Command | What it does |
|---------|-------------|
| `systemctl status polybot` | Check if bot is running |
| `systemctl restart polybot` | Restart the bot |
| `systemctl stop polybot` | Stop the bot |
| `journalctl -u polybot -f` | Live tail the logs |
| `journalctl -u polybot -n 100` | Last 100 log lines |
| `cat /root/polybot/logs/systemd.log` | Full log file |

---

## Updating the Bot

When code changes are made locally:

```powershell
# From your Windows machine — upload updated files
scp "C:\Users\Owner\OneDrive\Desktop\Polybot Snipez\web_dashboard.py" root@<VPS_IP>:/root/polybot/
scp "C:\Users\Owner\OneDrive\Desktop\Polybot Snipez\polymarket_client.py" root@<VPS_IP>:/root/polybot/

# Then restart on VPS
ssh root@<VPS_IP> "systemctl restart polybot"
```

---

## Security Notes

- The dashboard is password-protected when `DASHBOARD_PASSWORD` is set in `.env`
- Only ports 22 (SSH) and 5050 (dashboard) are open
- Your `.env` contains API keys — never expose it publicly
- For extra security, you can change the dashboard port in `web_dashboard.py` (last line)
