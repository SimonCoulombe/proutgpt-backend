# ProutGPT Backend 💨

Flask proxy backend for [ProutGPT](https://proutgpt.com) — the world's most sophisticated fart-joke chatbot.

This repo contains:
- A **Flask proxy** that forwards requests to OpenRouter (cloud LLMs) with parallel model racing and sequential fallbacks
- **SSE streaming** endpoint for real-time token-by-token output
- **Per-IP rate limiting** to prevent abuse
- **Input validation** with sensible limits
- **Nginx config** to route traffic from your domain to Flask and local Ollama
- A **systemd + gunicorn** service for production-grade process management
- A **one-shot setup script** for provisioning a fresh Oracle Cloud VM

---

## Architecture

```
Browser (proutgpt-chat SPA)
        │
        ├── POST /api/openrouter  ──► nginx :80/:443 ──► gunicorn/Flask :5000 ──► OpenRouter API
        │   (streaming SSE or JSON)
        │
        ├── POST /api/generate   ──► (same, legacy compat)
        │
        ├── GET  /models          ──► returns available model list
        │
        ├── GET  /health          ──► liveness check
        │
        └── GET  /api/tags        ──► nginx ──► Ollama :11434  (local models)
```

### Model fallback strategy

On every request the backend attempts models in this order:

1. **User-requested model** — if it returns 200, done.
2. **Race** — 3 small fast models called in parallel (`ThreadPoolExecutor`); first 200 wins.
3. **Sequential fallbacks** — 4 more models tried one by one if the race also fails.

This makes the free-tier OpenRouter experience essentially always return a response even under heavy rate-limiting.

---

## Provisioning a new Oracle Cloud VM (Free Tier)

### 1. Create the VM

Go to **Oracle Cloud Console → Compute → Instances → Create Instance**.

| Setting | Value |
|---|---|
| **Image** | Canonical Ubuntu 22.04 |
| **Shape** | Ampere `VM.Standard.A1.Flex` |
| **OCPUs** | 4 |
| **Memory** | 24 GB |
| **Subnet** | Public subnet |
| **Public IP** | Assign a public IPv4 address |
| **SSH keys** | Upload your public key |

### 2. Open ports — Oracle VCN Security List

```bash
SECLIST_OCID="<YOUR_SECURITY_LIST_OCID>"

oci network security-list update \
  --security-list-id "$SECLIST_OCID" \
  --ingress-security-rules '[
    {"protocol":"6","source":"0.0.0.0/0","tcpOptions":{"destinationPortRange":{"min":22,   "max":22}}},
    {"protocol":"6","source":"0.0.0.0/0","tcpOptions":{"destinationPortRange":{"min":80,   "max":80}}},
    {"protocol":"6","source":"0.0.0.0/0","tcpOptions":{"destinationPortRange":{"min":443,  "max":443}}},
    {"protocol":"6","source":"0.0.0.0/0","tcpOptions":{"destinationPortRange":{"min":5000, "max":5000}}}
  ]' \
  --force
```

> Port 5000 can be removed from the security list once nginx is confirmed working — all public traffic should go through 80/443.

### 3. SSH in

```bash
ssh ubuntu@<VM_PUBLIC_IP>
```

---

## Initial Setup on the VM

### Option A — One-shot automated setup

```bash
git clone https://github.com/simoncoulombe/proutgpt-backend.git
cd proutgpt-backend
bash setup_vm.sh
```

### Option B — Manual step-by-step

#### System packages

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv nginx curl git iptables-persistent
```

#### Python virtualenv + dependencies

```bash
python3 -m venv ~/openrouter-env
~/openrouter-env/bin/pip install -r requirements.txt
# requirements.txt includes: flask, flask-cors, requests, gunicorn
```

#### API key

Two files are needed — one for interactive use, one for systemd:

```bash
# For start_openrouter.sh / interactive use
echo 'export OPENROUTER_API_KEY=sk-or-v1-REPLACE_ME' > ~/.env
chmod 600 ~/.env

# For systemd (KEY=value, no 'export')
echo 'OPENROUTER_API_KEY=sk-or-v1-REPLACE_ME' | sudo tee /etc/proutgpt.env
sudo chmod 600 /etc/proutgpt.env
```

#### OS-level firewall (iptables)

Oracle VMs have an OS firewall in addition to the VCN security list:

```bash
sudo iptables -I INPUT -p tcp --dport 80   -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443  -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 5000 -j ACCEPT
sudo netfilter-persistent save
```

---

## Nginx Setup

```bash
sudo cp nginx/proutgpt.conf /etc/nginx/sites-available/proutgpt.conf
sudo ln -s /etc/nginx/sites-available/proutgpt.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx
```

### HTTPS with Let's Encrypt

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.proutgpt.com
# Auto-renewal is set up by certbot via a systemd timer
```

---

## Ollama Setup (local LLMs)

```bash
# Install
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama

# Pull a model
ollama pull llama3.2

# Test
curl http://localhost:11434/api/generate \
  -d '{"model":"llama3.2","prompt":"dis bonjour","stream":false}'
```

The nginx config proxies `/ollama/` → `localhost:11434`.
Set the frontend API URL to `https://api.proutgpt.com/ollama` and select the Ollama backend.

---

## Systemd Service (production)

The service runs the Flask app under **gunicorn** (4 workers, 120 s timeout):

```bash
# Install and enable
sudo cp proutgpt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proutgpt

# Day-to-day operations
sudo systemctl status proutgpt        # check status
sudo systemctl restart proutgpt       # restart after code changes
journalctl -u proutgpt -f             # follow live logs
```

---

## Running Locally (development)

```bash
export OPENROUTER_API_KEY=sk-or-v1-...

# Option 1 — helper script (creates venv, installs deps, starts Flask dev server)
bash start_openrouter.sh

# Option 2 — direct gunicorn (mirrors production)
~/openrouter-env/bin/gunicorn --workers 2 --bind 0.0.0.0:5000 openrouter_proxy:app

# Option 3 — plain python (single-threaded, debug mode)
~/openrouter-env/bin/python openrouter_proxy.py
```

Server listens on `http://0.0.0.0:5000`.

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | **Yes** | — | OpenRouter secret key (`sk-or-v1-…`) |
| `RATE_LIMIT_MAX` | No | `20` | Max requests per IP per window |
| `RATE_LIMIT_WINDOW` | No | `60` | Rate-limit window in seconds |

---

## API Reference

### `POST /api/openrouter`

Send a chat message (main endpoint used by the frontend).

**Request body:**
```json
{
  "messages": [
    {"role": "user", "content": "Fais une blague de prout"}
  ],
  "model": "z-ai/glm-4.5-air:free",
  "userMessageCount": 1,
  "stream": false
}
```

Set `"stream": true` to receive a Server-Sent Events (SSE) stream instead of a JSON response.

**Response (non-streaming):**
```json
{
  "response": "Pourquoi les pétomanes font-ils du yoga ? ...",
  "done": true,
  "model": "z-ai/glm-4.5-air:free"
}
```

**Response (streaming, `Content-Type: text/event-stream`):**
```
data: {"token": "Pourquoi"}
data: {"token": " les"}
data: {"token": " pétomanes"}
...
data: {"done": true, "model": "z-ai/glm-4.5-air:free"}
data: [DONE]
```

### `POST /api/generate`

Legacy Ollama-compatible endpoint — identical behaviour to `/api/openrouter`.

### `GET /models`

Returns the full list of available OpenRouter models.

```json
{
  "race_models": ["liquid/lfm-2.5-1.2b-instruct:free", "..."],
  "fallback_models": ["z-ai/glm-4.5-air:free", "..."],
  "all_models": ["..."]
}
```

### `GET /health`

Liveness check. Returns `{"status": "ok"}`.

---

## Validation limits

| Field | Limit |
|---|---|
| Messages in history | 40 max |
| Characters per message | 4 000 max |
| Requests per IP / 60 s | 20 (configurable via env) |

---

## File Structure

```
proutgpt-backend/
├── openrouter_proxy.py     # Flask app — proxy, streaming, rate-limiting
├── requirements.txt        # flask, flask-cors, requests, gunicorn
├── proutgpt.service        # systemd unit (gunicorn, 4 workers)
├── start_openrouter.sh     # Dev/manual start script
├── setup_vm.sh             # One-shot Oracle Cloud VM provisioning
├── .env.example            # Template for ~/.env
├── .gitignore
├── nginx/
│   └── proutgpt.conf       # Nginx reverse-proxy + SSE buffering disabled
└── README.md
```

---

## Security Notes

- **API key is never in the repo** — stored in `~/.env` (dev) or `/etc/proutgpt.env` (systemd).
- Rate limiting (20 req / 60 s per IP) is enforced in-process; add nginx `limit_req` for extra protection.
- Port 5000 should be closed in the VCN security list in production (traffic goes through nginx on 443 only).
- Ollama port 11434 should be firewalled unless you intentionally expose it.
- `CORS(app)` allows all origins — restrict to your frontend domain in production if needed.

---

## Credits

Vibe-coded by **Benoît Coulombe**, **Gaëlle Coulombe** et **Simon Coulombe**.  
Propulsé par des modèles IA gratuits et des blagues de pets 💨
