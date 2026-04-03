# ProutGPT Backend

Backend for [ProutGPT](https://github.com/simoncoulombe/proutgpt-chat) — the world's most sophisticated AI fart-joke chatbot.

This repo contains:
- A **Flask proxy** that forwards requests to OpenRouter (cloud LLMs)
- **Nginx config** to route traffic from your domain to the Flask API and local Ollama
- A **systemd service** so the backend starts automatically at boot
- A **one-shot setup script** for provisioning a fresh Oracle Cloud VM
- Instructions for setting up **Ollama** to run local LLMs

---

## Architecture

```
Browser (proutgpt-chat)
        │
        ├─── /api/openrouter  ──►  nginx (port 80/443)
        │                               │
        │                               ├─► Flask proxy (port 5000)
        │                               │       │
        │                               │       └─► OpenRouter API (cloud)
        │
        └─── /api/generate   ──►  nginx  ──►  Ollama (port 11434)
             /api/tags
```

---

## Provisioning a new Oracle Cloud VM (Free Tier)

### 1. Create the VM

Go to **Oracle Cloud Console → Compute → Instances → Create Instance**.

| Setting | Value |
|---|---|
| **Name** | `proutgpt-backend` |
| **Image** | Canonical Ubuntu 22.04 |
| **Shape** | Ampere `VM.Standard.A1.Flex` |
| **OCPUs** | 4 |
| **Memory** | 24 GB |
| **Subnet** | Public subnet |
| **Public IP** | Assign a public IPv4 address |
| **SSH keys** | Upload your public key |

Click **Create**.

### 2. Open ports — Oracle VCN Security List (via OCI CLI)

The OCI Console web UI works, but the CLI is faster and repeatable.

**Install the OCI CLI** (if not already done):
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
oci setup config    # follow the prompts
```

**Find your VCN and security list IDs:**
```bash
# List compartments (get your compartment OCID)
oci iam compartment list --all --query 'data[*].{Name:name,OCID:id}' --output table

# List VCNs in your compartment
oci network vcn list --compartment-id <YOUR_COMPARTMENT_OCID> \
  --query 'data[*].{Name:"display-name",OCID:id}' --output table

# List security lists in the VCN
oci network security-list list \
  --compartment-id <YOUR_COMPARTMENT_OCID> \
  --vcn-id <YOUR_VCN_OCID> \
  --query 'data[*].{Name:"display-name",OCID:id}' --output table
```

**Add ingress rules for HTTP, HTTPS, and Ollama:**
```bash
SECLIST_OCID="<YOUR_SECURITY_LIST_OCID>"

# Get existing ingress rules first (don't overwrite them!)
EXISTING=$(oci network security-list get --security-list-id "$SECLIST_OCID" \
  --query 'data."ingress-security-rules"')

# Add HTTP (80), HTTPS (443), Flask (5000), Ollama (11434)
oci network security-list update \
  --security-list-id "$SECLIST_OCID" \
  --ingress-security-rules "[
    {\"protocol\": \"6\", \"source\": \"0.0.0.0/0\", \"tcpOptions\": {\"destinationPortRange\": {\"min\": 22,    \"max\": 22}}},
    {\"protocol\": \"6\", \"source\": \"0.0.0.0/0\", \"tcpOptions\": {\"destinationPortRange\": {\"min\": 80,    \"max\": 80}}},
    {\"protocol\": \"6\", \"source\": \"0.0.0.0/0\", \"tcpOptions\": {\"destinationPortRange\": {\"min\": 443,   \"max\": 443}}},
    {\"protocol\": \"6\", \"source\": \"0.0.0.0/0\", \"tcpOptions\": {\"destinationPortRange\": {\"min\": 5000,  \"max\": 5000}}},
    {\"protocol\": \"6\", \"source\": \"0.0.0.0/0\", \"tcpOptions\": {\"destinationPortRange\": {\"min\": 11434, \"max\": 11434}}}
  ]" \
  --force
```

> **Note:** Protocol `6` = TCP. This replaces the entire ingress list, so make sure to include SSH (22) and any other existing rules you want to keep.

### 3. SSH into the VM

```bash
ssh ubuntu@<VM_PUBLIC_IP>
```

---

## Initial Setup on the VM

### Option A — One-shot automated setup

```bash
# Clone this repo
git clone https://github.com/simoncoulombe/proutgpt-backend.git
cd proutgpt-backend

# Create your API key file (never committed to git)
echo 'export OPENROUTER_API_KEY=sk-or-v1-...' > ~/.env

# Run the setup script
bash setup_vm.sh
```

This script will:
- Install Python 3, nginx, curl, git
- Create a Python virtualenv and install Flask dependencies
- Install Ollama and pull `llama3.2`
- Deploy the nginx config
- Install and enable the `proutgpt` systemd service

### Option B — Manual step-by-step

#### System packages

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv nginx curl git iptables-persistent
```

#### Python virtualenv

```bash
python3 -m venv ~/openrouter-env
~/openrouter-env/bin/pip install -r requirements.txt
```

#### API key (never committed to git)

```bash
echo 'export OPENROUTER_API_KEY=sk-or-v1-REPLACE_ME' > ~/.env
chmod 600 ~/.env
```

#### Oracle internal firewall (iptables)

Oracle VMs also have an OS-level firewall. Open the ports:

```bash
sudo iptables -I INPUT -p tcp --dport 80    -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443   -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 5000  -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 11434 -j ACCEPT
sudo netfilter-persistent save
```

---

## Nginx Setup

The config is at `nginx/proutgpt.conf`.

```bash
# Edit domain name if needed
sudo nano nginx/proutgpt.conf   # replace api.proutgpt.com with your domain or IP

# Install the config
sudo cp nginx/proutgpt.conf /etc/nginx/sites-available/proutgpt.conf
sudo ln -s /etc/nginx/sites-available/proutgpt.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # remove default site
sudo nginx -t && sudo systemctl restart nginx
```

### Add HTTPS with Let's Encrypt

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.proutgpt.com
```

Certbot will automatically update the nginx config and set up a renewal cron job.

---

## Ollama Setup (local LLMs)

### Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable ollama
sudo systemctl start ollama
```

### Pull the llama3.2 model

```bash
ollama pull llama3.2
```

This downloads ~2 GB. On a 24 GB Ampere VM it runs at a decent speed.

### Test Ollama directly

```bash
curl http://localhost:11434/api/generate \
  -d '{"model":"llama3.2","prompt":"dis bonjour","stream":false}'
```

### Expose Ollama via nginx

The nginx config already proxies `/ollama/` → `localhost:11434`. From the frontend, set the API URL to `https://api.proutgpt.com/ollama` and choose the Ollama backend.

---

## Systemd Service (auto-start on boot)

```bash
# Install the service
sudo cp proutgpt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable proutgpt
sudo systemctl start proutgpt

# Check status
sudo systemctl status proutgpt

# View logs
journalctl -u proutgpt -f
# or
tail -f ~/openrouter.log
```

The service reads `~/.env` for the API key via `EnvironmentFile`.

---

## Running Locally (development)

```bash
# Set your API key
export OPENROUTER_API_KEY=sk-or-v1-...

# Start the server
bash start_openrouter.sh
# or directly:
~/openrouter-env/bin/python openrouter_proxy.py
```

The server listens on `http://0.0.0.0:5000`.

---

## API Reference

### `POST /api/openrouter`

Proxy a chat message to OpenRouter.

**Request body:**
```json
{
  "prompt": "Fais une blague de prout",
  "model": "z-ai/glm-4.5-air:free"
}
```

**Response:**
```json
{
  "response": "Pourquoi les pétomanes vont-ils à l'opéra ? ...",
  "done": true,
  "model": "z-ai/glm-4.5-air:free"
}
```

### `POST /api/generate`

Legacy Ollama-compatible endpoint (also proxies to OpenRouter).

### `GET /health`

Returns `{"status": "ok"}`.

---

## File Structure

```
proutgpt-backend/
├── openrouter_proxy.py     # Flask app — the actual proxy
├── start_openrouter.sh     # Start script for manual / dev use
├── setup_vm.sh             # One-shot VM provisioning script
├── requirements.txt        # Python dependencies
├── proutgpt.service        # systemd service definition
├── .env.example            # Template for ~/.env (copy, fill, keep private)
├── .gitignore
├── nginx/
│   └── proutgpt.conf       # Nginx reverse-proxy config
└── README.md
```

---

## Security Notes

- **The OpenRouter API key is stored in `~/.env` on the server — never in the repo.**
- `~/.env` is in `.gitignore` and loaded by the systemd service via `EnvironmentFile`.
- Consider restricting port `5000` in the nginx config and VCN security list once nginx is working (all traffic should go through nginx on 80/443).
- Ollama port `11434` is only needed if you expose it directly; otherwise close it in the VCN security list.

---

## Credits

Vibe-coded by Benoît Coulombe, Gaëlle Coulombe et Simon Coulombe.  
Propulsé par des modèles IA gratuits et des blagues de pets.
