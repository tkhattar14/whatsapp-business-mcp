# Deploying to a VM

These steps assume an **Ubuntu 24.04** VM (Python 3.12 is included) with a public IP,
and a domain/subdomain you control, e.g. `support.maxmigold.com`.

```
Internet ──HTTPS──▶ Caddy (ports 80/443, automatic certificate)
                      └─▶ 127.0.0.1:8080  main_http.py  (systemd service "whatsapp-bot")
                                             └─ data/support.db (tickets, conversations, relay links)
```

## 1. DNS

Create an **A record** for `support.maxmigold.com` pointing to the VM's public IP. Wait until
`nslookup support.maxmigold.com` returns that IP.

## 2. Install packages (on the VM)

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip caddy ufw
```

## 3. Get the code

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin whatsapp
sudo git clone https://github.com/Max-Migold-Ltd/whatsapp-business-mcp.git /opt/whatsapp-business-mcp
cd /opt/whatsapp-business-mcp
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo mkdir -p data
```

(If the GitHub repo is private, clone with a personal access token or a deploy key.)

## 4. Copy the configuration and the database from the current PC

**Stop the server on the PC first** (so the database file is complete), then from the PC (PowerShell, in the project folder):

```powershell
scp .env data\support.db youruser@VM_IP:/tmp/
```

Copying `support.db` keeps the ticket numbering (MMF-00003 next, not MMF-00001 again) and
the agent's swipe-reply links. Then on the VM:

```bash
sudo mv /tmp/.env /opt/whatsapp-business-mcp/.env
sudo mv /tmp/support.db /opt/whatsapp-business-mcp/data/support.db
sudo chown -R whatsapp:whatsapp /opt/whatsapp-business-mcp
sudo chmod 600 /opt/whatsapp-business-mcp/.env
```

`.env` holds the Meta token, app secret and Microsoft secret — never commit it or leave copies in `/tmp`.

## 5. Run it as a service

```bash
sudo cp deploy/whatsapp-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now whatsapp-bot
sudo systemctl status whatsapp-bot          # should say "active (running)"
curl http://127.0.0.1:8080/health           # should return {"status":"healthy",...}
```

## 6. HTTPS with Caddy

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile               # replace support.example.com with your domain
sudo systemctl reload caddy
```

Only `/webhook` and `/health` are exposed publicly; everything else returns 404.

## 7. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

Also allow ports 80 and 443 in the cloud provider's firewall / security group.
Check from your PC: `https://support.maxmigold.com/health` should show `"healthy"`.

## 8. Point Meta at the VM

Meta App Dashboard → **WhatsApp → Configuration → Webhook → Edit**:

- **Callback URL:** `https://support.maxmigold.com/webhook`
- **Verify token:** the `WEBHOOK_VERIFY_TOKEN` value from `.env` (unchanged)
- **Verify and save** — `messages` stays subscribed.

Then **stop the server and the cloudflared tunnel on the PC** — the VM now handles everything.

## 9. Test

Message the business number and lodge a complaint, check the agent receives it, swipe-reply,
and check the Excel file. Watch the log while testing:

```bash
sudo journalctl -u whatsapp-bot -f
```

## Day-to-day

| Task | Command |
|---|---|
| View logs | `sudo journalctl -u whatsapp-bot -f` |
| Restart (e.g. after editing `.env` or `flow.json`) | `sudo systemctl restart whatsapp-bot` |
| Update to the latest code | `cd /opt/whatsapp-business-mcp && sudo -u whatsapp git pull && sudo .venv/bin/pip install -r requirements.txt && sudo systemctl restart whatsapp-bot` |
| Back up the database | `sudo sqlite3 /opt/whatsapp-business-mcp/data/support.db ".backup '/root/support-$(date +%F).db'"` (install `sqlite3` first) |
| Download tickets as CSV | From your PC: `ssh -L 8080:127.0.0.1:8080 youruser@VM_IP`, then open `http://localhost:8080/api/v1/support/tickets.csv` with the `X-API-Key` header |

Back up `data/support.db` regularly (e.g. a daily cron job) — Excel has the log, but the database
also holds conversation state and the agent's reply links.
