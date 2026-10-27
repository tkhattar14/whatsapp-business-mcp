# WhatsApp Business MCP Server

A security-hardened [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for the official WhatsApp Business Cloud API. Connect AI assistants like Claude, Cursor, or any MCP-compatible client to WhatsApp Business.

## Features

- **50+ tools** across 8 modules — messaging, templates, media, analytics, flows, webhooks, business profile, account management
- **Security-first** — API key auth, rate limiting, input validation, webhook signature verification, error sanitization, path traversal protection
- **Official Meta API only** — all MCP tool and support bot calls go to `graph.facebook.com`, no unofficial/third-party APIs (the optional group agent below is the exception)
- **Two transport modes** — stdio (for MCP clients like Claude Desktop) and HTTP (for REST API access)
- **Production-ready** — CORS controls, security headers, configurable via environment variables
- **Customer support bot** — intake questionnaire with handoff to a human agent in the WhatsApp Business app, conversations and tickets logged to SharePoint Excel. See [SUPPORT_BOT.md](SUPPORT_BOT.md); deployment: [DEPLOY.md](DEPLOY.md)
- **WhatsApp group agent** (optional) — a dedicated number in residents' WhatsApp groups that logs each issue, reminds staff, asks for a 0-10 rating when the job is done and records it all in SharePoint Excel; runs on a self-hosted or hosted LLM, with a fallback model (Gemini) if the main one fails. It connects as a linked device, which WhatsApp does not officially permit. See [GROUP_AGENT.md](GROUP_AGENT.md)

## Tools Overview

| Module | Tools | Description |
|--------|-------|-------------|
| **Messaging** | Send text, media, interactive, location, contact, reaction, reply | All WhatsApp message types |
| **Templates** | Create, list, send, delete, get status | Full template lifecycle management |
| **Media** | Upload, download, delete, get info | Image, video, audio, document handling |
| **Analytics** | Conversation analytics, quality rating, messaging limits | Business insights |
| **Flows** | Create, list, send flow messages | WhatsApp interactive experiences |
| **Webhooks** | Subscribe, unsubscribe, manage webhook fields | Event management |
| **Business Profile** | Get/update profile, phone numbers | Business identity management |
| **Business Account** | WABA management, phone operations | Account administration |

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/tkhattar14/whatsapp-business-mcp.git
cd whatsapp-business-mcp
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your Meta WhatsApp Business API credentials
# Get them from: https://developers.facebook.com/
```

### 3. Run (stdio mode — for MCP clients)

```bash
source .env && python main.py
```

### 4. Run (HTTP mode — for REST API)

```bash
source .env && python main_http.py
```

## MCP Client Configuration

### Claude Desktop / Cursor

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "python",
      "args": ["/path/to/whatsapp-business-mcp/main.py"],
      "env": {
        "META_ACCESS_TOKEN": "your_token",
        "META_PHONE_NUMBER_ID": "your_phone_id"
      }
    }
  }
}
```

### mcporter (OpenClaw)

```bash
mcporter add whatsapp-business --stdio "python /path/to/main.py"
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `META_ACCESS_TOKEN` | Yes | Meta API bearer token |
| `META_PHONE_NUMBER_ID` | Yes | WhatsApp sender phone number ID |
| `META_BUSINESS_ACCOUNT_ID` | Recommended | Business account ID |
| `WABA_ID` | Recommended | WhatsApp Business Account ID |
| `META_APP_ID` | Optional | App ID for webhook management |
| `META_APP_SECRET` | Recommended | App secret for webhook signature verification |
| `MCP_API_KEY` | Recommended | API key for HTTP endpoint auth |
| `ALLOWED_ORIGINS` | Optional | Comma-separated CORS origins |
| `MEDIA_UPLOAD_DIR` | Optional | Allowed media upload directory (default: `/tmp/whatsapp-media/`) |
| `WHATSAPP_API_VERSION` | Optional | Graph API version (default: `v22.0`) |

## Security

This server is security-hardened with:

- **API key authentication** on HTTP endpoints (`X-API-Key` header)
- **Rate limiting** (10 req/sec per IP, in-memory token bucket)
- **Webhook signature verification** (HMAC-SHA256 via `META_APP_SECRET`)
- **Input validation** — phone number format, message length limits
- **Error sanitization** — tokens and sensitive URLs stripped from error responses
- **Path traversal protection** — media uploads restricted to allowed directory
- **Security headers** — `X-Content-Type-Options`, `X-Frame-Options`, `Cache-Control`
- **No wildcard CORS** — explicit origin allowlist only

See [SECURITY.md](SECURITY.md) for full details.

## API Version

Uses WhatsApp Cloud API **v22.0** (latest stable, March 2026). Configurable via `WHATSAPP_API_VERSION` env var.

## License

MIT — see [LICENSE](LICENSE).
