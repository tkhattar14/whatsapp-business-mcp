# Max Migold WhatsApp Support Bot

Residents message the Max Migold WhatsApp number and a bot asks a few questions
(location, complaint or feedback, service area, details). Each completed
conversation becomes a **ticket** (e.g. `MMF-00042`) in a SharePoint Excel
workbook, and is **forwarded to the agent's own WhatsApp**. The agent replies by
swiping-reply, and the resident receives the answer from the business number.

```
Resident ──WhatsApp──▶ Meta ──webhook──▶ main_http.py /webhook
                                           ├─ asks the questions in flow.json
                                           ├─ saves everything in data/support.db
                                           ├─ every 15s → SharePoint Excel (Tickets + Conversations)
                                           └─ forwards tickets & follow-ups ──▶ Agent's WhatsApp
Agent swipe-replies ──▶ server ──▶ resident gets it from the business number
```

One deployment serves one business number. For another business line, deploy
another copy with its own `.env`.

## The conversation

Defined in [support_bot/flow.json](support_bot/flow.json):

```
Bot:  Hello Ada! 👋 Welcome to the Max Migold Facility Management support channel.
      (To check an existing request, just send your ticket ID, e.g. MMF-00012.)
      Which location are you contacting us from?          [La Tour] [Lagoon] [Seplat Eket]

        La Tour            → "What is your flat number? (1 - 45)"   (only 1–45 accepted)
        Lagoon/Seplat Eket → no follow-up question

Bot:  Would you like to lodge a complaint or give feedback?  [Lodge a complaint] [Give feedback]
Bot:  Which service area is your complaint/feedback about?  [Choose service area ▾]
      Plumbing · Cleaning · Security · HVAC · Civil Work · Waste Management · Landscaping · Electrical · Others

  Complaint → "Please describe your complaint in full. You can also send a photo."
              "✅ Your complaint has been logged… Your ticket ID is MMF-00042."
  Feedback  → "Please share your feedback about our service."
              "🙏 Your feedback has been received (reference MMF-00043)…"
```

- The complaint text goes in the **Complaint** column and feedback in a separate **Feedback** column.
- Residents can tap buttons, pick from the list, or type the option number or name ("AC" selects HVAC). A wrong answer is asked again.
- Typing `menu`, `restart` or `start over` begins again. An unfinished conversation starts over after `GREETING_GAP_HOURS` (default 24).

### Ticket status

- The agent sets **Status** in the Excel Tickets table with the dropdown: **Pending → In Progress → Resolved**.
- A resident sends their ticket ID (`MMF-00042`, `mmf 42`, `status MMF-00042`) at any time and gets the live status from Excel plus a summary.
- **Residents can only check tickets from their own phone number** — IDs are sequential, so otherwise anyone could look up a neighbour's complaint.
- If SharePoint can't be reached, the bot replies with the last status it knew.

## How the agent replies (relay mode)

Set `AGENT_WHATSAPP_NUMBER` to the agent's **personal WhatsApp number** (not the business number).

**The agent receives:**
```
🆕 New complaint MMF-00042
👤 Ada Obi · +2348012345678

• Location: La Tour
• Flat Number: 12
• Request Type: Complaint
• Service Area: Plumbing
• Complaint: The kitchen sink has been leaking since yesterday.

↩️ Swipe-reply to this message to answer the resident.
```
…plus any photo the resident sent, and every later message from that resident (`💬 MMF-00042 · Ada Obi: …`).

**To answer:** swipe-reply (or long-press → Reply) on any forwarded message and type. Photos and documents can be sent the same way. The agent can also start a message with the ticket ID instead: `MMF-00042 A plumber will come at 2pm`. A ✅ reaction confirms the reply was delivered; if it couldn't be, the agent gets a ❌ message saying why.

**Meta's 24-hour rule (important):** the business number can only message the agent if the **agent has messaged the business number in the last 24 hours**. So the agent should send a quick "hi" to the business number at the start of each day/shift. If forwards can't be delivered, they are **queued** and delivered as soon as the agent sends anything. Optionally, an approved alert template (`AGENT_ALERT_TEMPLATE`) notifies the agent that forwards are waiting — see below.

The same rule applies to residents: the agent can only reply within 24 hours of the resident's last message.

When the agent has been quiet for `AGENT_IDLE_HOURS` (default 12), the bot takes the conversation back on the resident's next message.

### Optional: agent alert template

Create a **Utility** template in WhatsApp Manager (or ask for it to be created via the API), e.g. name `agent_new_messages`, language English, body:

> You have new WhatsApp support messages waiting. Reply to this message to receive them.

Once Meta approves it, set `AGENT_ALERT_TEMPLATE=agent_new_messages`. The agent's reply opens the 24-hour window and the queued messages arrive.

## Changing the questions

Edit [support_bot/flow.json](support_bot/flow.json) and restart the server — no code changes needed. The server refuses to start if the file has a mistake and says what's wrong.

| Key | Meaning |
|---|---|
| `greeting` | Shown above the first question. `{name}` = resident's first name |
| `start` | ID of the first step |
| `steps` | The questions. Each has `type` (`choice` or `text`), `question`, and `field` (the Excel column for the answer) |
| `options` | For `choice` steps: `id`, `title`, optional `value` (what's recorded, if different from the button text), `next` (to branch) and `keywords`. Up to 3 options show as buttons (titles ≤ 20 characters), 4–10 as a list (≤ 24 characters) |
| `next` | The step after this one, or `"done"` to finish. On an option, it overrides the step's `next` |
| `min` / `max` | For `text` steps: only accept a whole number in this range, with `invalid_answer` as the retry message |
| `done` | Final message (on a step, it overrides the global one for that branch). `{ticket}` = ticket ID, `{summary}` = the answers |
| `ticket_prefix` / `ticket_status` / `ticket_statuses` | IDs look like `MMF-00042`; new tickets start as `Pending`; allowed statuses `Pending`, `In Progress`, `Resolved` |
| `status_reply` / `status_not_found` | Replies to a ticket status check |

**If you add, remove or rename a `field`, regenerate the workbook** (or change the Tickets headers to match) — the server log prints the exact column list at startup.

## Setup

### 1. Meta / WhatsApp (direct Cloud API)

1. In **business.facebook.com**, create the Business Portfolio and start **Business Verification**.
2. In **developers.facebook.com**, create an app (type *Business*), connect it to the portfolio, and add **WhatsApp**.
3. Add the phone number in the app's **WhatsApp → API Setup** (it must not be registered on the WhatsApp or WhatsApp Business app).
4. **Business Settings → Users → System users**: create one (Admin), assign it the app and the WhatsApp account with **Full control**, and **Generate new token** (expiry *Never*, permissions `whatsapp_business_messaging` + `whatsapp_business_management`). Put it in `META_ACCESS_TOKEN`; the number's ID in `META_PHONE_NUMBER_ID`; the account ID in `WABA_ID`.
5. Copy the **App Secret** (App settings → Basic) into `META_APP_SECRET`, and pick any random string for `WEBHOOK_VERIFY_TOKEN`.
6. With the server running on a public **HTTPS** URL, in **WhatsApp → Configuration**: Callback URL `https://your-server/webhook`, Verify token = `WEBHOOK_VERIFY_TOKEN`, then subscribe to the **`messages`** field.
7. Make sure the app is **subscribed to the WhatsApp account** (`POST /{WABA_ID}/subscribed_apps` — the `subscribe_to_waba` tool does this).
8. **Publish** the app (needs a privacy policy URL, icon and category) so real messages are delivered.

### 2. SharePoint Excel log

1. **Create the workbook** (columns come from flow.json, so they always match the bot):
   ```bash
   pip install openpyxl
   python scripts/create_excel_log.py
   ```
   `Complaints Log.xlsx` has:
   - **Tickets**: `Ticket # | Date/Time | Phone | Customer Name | Location | Flat Number | Request Type | Service Area | Complaint | Feedback | Status`, with the Status dropdown and colours
   - **Conversations**: every message
   - **How to use**: rules for the agent (don't rename headers, tables or the file)
2. Upload it to SharePoint, open it, click **Share → Copy link**, and put the link in `SHAREPOINT_FILE_URL`. (Alternatively set `SHAREPOINT_HOSTNAME`, `SHAREPOINT_SITE_PATH` and `SHAREPOINT_FILE_PATH` — the file's path inside the site's *Documents* library.)
3. Register an app in **Microsoft Entra admin center → App registrations**, add a **client secret**, and under API permissions add Microsoft Graph **Application** permissions, then **Grant admin consent**:
   - Simplest: **`Sites.ReadWrite.All`** — works with the sharing link.
   - Most restrictive: **`Sites.Selected`**, plus the admin granting write access to just this site (`Grant-PnPAzureADAppSitePermission`). With `Sites.Selected`, use the hostname/site/file-path settings rather than the sharing link.
4. Fill in `SHAREPOINT_TENANT_ID`, `SHAREPOINT_CLIENT_ID`, `SHAREPOINT_CLIENT_SECRET`.

Rows are written every 15 seconds and it works while people have the file open in Excel for the web. If SharePoint is unavailable, data waits in `data/support.db` and is written once it recovers (look for `SharePoint … sync failed` in the log). Text starting with `=`, `+`, `-` or `@` is prefixed with `'` so it can't run as an Excel formula.

### 3. Run / deploy

```bash
pip install -r requirements.txt
cp .env.example .env    # then fill it in
python main_http.py     # listens on PORT (default 8080)
```

Run it behind HTTPS (reverse proxy, or a platform like Railway/Render/Azure App Service). For local testing, `cloudflared tunnel --url http://localhost:8080` gives a temporary HTTPS URL. Keep `data/support.db` on persistent storage — it holds conversation state, tickets and the relay links.

### 4. Useful endpoints

All require `MCP_API_KEY` to be set and sent as the `X-API-Key` header.

| Endpoint | What it returns |
|---|---|
| `GET /api/v1/support/conversations` | Conversations with their state (bot/agent) and current question |
| `GET /api/v1/support/tickets` | Tickets with all answers |
| `GET /api/v1/support/conversations/{phone}/messages` | Full message history for one resident |
| `GET /api/v1/support/export.csv` | All messages as CSV (backup if SharePoint is unavailable) |
| `GET /api/v1/support/tickets.csv` | All tickets as CSV |

## Tests

```bash
pip install pytest openpyxl
python -m pytest tests
```

The tests simulate signed Meta webhooks and mock the WhatsApp and Microsoft Graph APIs — no real messages are sent.

## Coexistence (alternative to relay mode)

If the number is instead connected through a Meta partner with **Coexistence**, leave `AGENT_WHATSAPP_NUMBER` empty: the agent replies in the WhatsApp Business app on the same number, and the server learns about those replies from the `smb_message_echoes` webhook field (subscribe to it as well).

## Limitations

- **One agent number.** Replies are logged as "Agent". More agents can be added later.
- **No out-of-hours message** yet.
- **Residents aren't notified automatically when the status changes** — they see it when they check, or the agent tells them.
