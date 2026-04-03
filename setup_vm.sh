#!/bin/bash
# setup_vm.sh — One-shot setup script for a fresh Oracle Cloud Ubuntu 22.04 VM
#
# Run as the default ubuntu user (has sudo):
#   bash setup_vm.sh
#
# What it does:
#   1. Updates system packages
#   2. Installs Python 3, pip, nginx
#   3. Creates a Python virtualenv and installs Flask dependencies
#   4. Installs Ollama and pulls llama3.2
#   5. Copies the nginx config
#   6. Installs and enables the systemd services
#   7. Opens required firewall ports via iptables (Oracle's internal firewall)

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="$HOME"
VENV_DIR="$HOME_DIR/openrouter-env"

echo "========================================"
echo "  ProutGPT Backend — VM Setup"
echo "========================================"

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/7] Updating system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv nginx curl git

# ── 2. Python virtualenv ──────────────────────────────────────────────────────
echo "[2/7] Setting up Python virtualenv..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

# ── 3. Environment file ───────────────────────────────────────────────────────
echo "[3/7] Checking environment file..."
if [ ! -f "$HOME_DIR/.env" ]; then
    echo "  WARNING: $HOME_DIR/.env not found!"
    echo "  Create it with your OpenRouter API key before starting the service:"
    echo "    echo 'export OPENROUTER_API_KEY=sk-or-v1-...' > ~/.env"
    echo "  Continuing setup..."
else
    echo "  ~/.env found."
fi

# ── 4. Install Ollama ─────────────────────────────────────────────────────────
echo "[4/7] Installing Ollama..."
if ! command -v ollama &>/dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
else
    echo "  Ollama already installed, skipping."
fi

# Enable and start Ollama service
sudo systemctl enable ollama
sudo systemctl start ollama

# Pull llama3.2 (this takes a few minutes on first run)
echo "  Pulling llama3.2 model (this may take several minutes)..."
ollama pull llama3.2 || echo "  WARNING: Could not pull llama3.2. Run 'ollama pull llama3.2' manually."

# ── 5. Nginx config ───────────────────────────────────────────────────────────
echo "[5/7] Configuring nginx..."
sudo cp "$REPO_DIR/nginx/proutgpt.conf" /etc/nginx/sites-available/proutgpt.conf

if [ ! -f /etc/nginx/sites-enabled/proutgpt.conf ]; then
    sudo ln -s /etc/nginx/sites-available/proutgpt.conf /etc/nginx/sites-enabled/proutgpt.conf
fi

# Remove default site if it exists to avoid port conflict
if [ -f /etc/nginx/sites-enabled/default ]; then
    sudo rm /etc/nginx/sites-enabled/default
fi

sudo nginx -t && sudo systemctl enable nginx && sudo systemctl restart nginx
echo "  Nginx configured and running."

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "[6/7] Installing ProutGPT systemd service..."
sudo cp "$REPO_DIR/proutgpt.service" /etc/systemd/system/proutgpt.service
sudo systemctl daemon-reload
sudo systemctl enable proutgpt
sudo systemctl start proutgpt || echo "  WARNING: Service failed to start (check that ~/.env is configured)."
echo "  Service installed. Check status: sudo systemctl status proutgpt"

# ── 7. Oracle Linux internal firewall (iptables) ──────────────────────────────
echo "[7/7] Opening ports in iptables (Oracle internal firewall)..."

open_port() {
    local PORT=$1
    local PROTO=${2:-tcp}
    if ! sudo iptables -C INPUT -p "$PROTO" --dport "$PORT" -j ACCEPT 2>/dev/null; then
        sudo iptables -I INPUT -p "$PROTO" --dport "$PORT" -j ACCEPT
        echo "  Opened $PROTO/$PORT"
    else
        echo "  $PROTO/$PORT already open"
    fi
}

open_port 22    tcp   # SSH (should already be open)
open_port 80    tcp   # HTTP / nginx
open_port 443   tcp   # HTTPS / nginx
open_port 5000  tcp   # Flask (optional, only if you skip nginx)
open_port 11434 tcp   # Ollama (only needed if exposing directly)

# Persist iptables rules across reboots
sudo apt-get install -y -qq iptables-persistent
sudo netfilter-persistent save
echo "  iptables rules saved."

echo ""
echo "========================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "    1. Edit the nginx config if needed:"
echo "         sudo nano /etc/nginx/sites-available/proutgpt.conf"
echo "         (replace 'api.proutgpt.com' with your domain or IP)"
echo "    2. Make sure ~/.env contains your API key:"
echo "         export OPENROUTER_API_KEY=sk-or-v1-..."
echo "    3. Restart the service:"
echo "         sudo systemctl restart proutgpt"
echo "    4. (Optional) Get HTTPS with Certbot:"
echo "         sudo apt install certbot python3-certbot-nginx"
echo "         sudo certbot --nginx -d api.proutgpt.com"
echo "========================================"
