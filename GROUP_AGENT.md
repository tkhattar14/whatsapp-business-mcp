# WhatsApp Group Agent

A dedicated WhatsApp number joins residents' groups as **Max Migold Assistant**. It reads every
message and, using an LLM you choose (any OpenAI-compatible API, with an optional fallback model such as
Gemini for when it is down), replies when needed.

```
Residents' groups ──▶ dedicated phone number, linked like WhatsApp Web
                        │
                        ▼
                wa_bridge/ (Node.js, Baileys)  ──every message──▶  group_agent/ (Python, in main_http.py)
                        ▲                                            ├─ stores it (group_messages table)
                        └──────────── reply ◀── send_reply tool ◀───┴─ asks the LLM: reply or stay silent?
```

## The issue workflow

```
Resident reports ──▶ agent logs it (MMF-G00012) and acknowledges in the group
                     (resident tagged a staff member? that person gets 10 min to answer first)
                         │
   no staff response ────┼── after 30 min ──▶ @mentions the "First contact" staff in the group
                         └── after 60 min ──▶ private message to the "Escalate to" staff
Staff responds ──────▶ In Progress   (not done after 48 h ──▶ private follow-up, then daily, 3 at most)
Staff says "done" ───▶ agent asks the resident in the group: "how likely are you to recommend us, 0-10?"
Resident rates ──────▶ Closed, score recorded for NPS   (no rating in 24 h ──▶ Closed - No Feedback)
```

- **Areas:** service jobs (Plumbing, Cleaning, Security, HVAC, Civil Work, Waste Management, Landscaping,
  Electrical, Others) get the 0-10 rating. Billing/Energy, Amenities & Bookings and General Enquiry close
  when staff say done, without a rating. The agent never discusses amounts or tariffs.

- The **LLM** reads the conversation and uses tools: `log_issue`, `update_issue` (staff responded / done),
  `record_feedback` (the rating) and `send_reply`. Only staff messages can move an issue on, and a staff
  message is never logged as an issue.
- **Plain code** runs the timers (reminders, escalation, closing unanswered surveys), so they are predictable.
  Safety nets: a staff reply quoting the resident's report counts as a response, and so does any staff message
  in the group before a reminder or a delayed acknowledgement is due; a bare "8" or "8/10" from the resident
  counts as their rating.
- The CEO ("Observer" in the Staff sheet) is never @mentioned or messaged automatically.

**Staff list:** the `Staff` sheet of the Complaints Log (Name, WhatsApp Number, Role, Escalation, Notes),
re-read every 10 minutes. Staff are recognised by their number (or by name where WhatsApp hides numbers).

**Records:** the `Group Issues` sheet of the Complaints Log - one row per issue, rewritten as it moves on
(opened, acknowledged, first staff response and response time, reminders and follow-ups, escalation, resolved by, score,
feedback, closed). The agent creates the sheet the first time it has an issue to record.

> **Risk:** WhatsApp does not permit automating a normal WhatsApp account this way; the number can
> be banned. Use a dedicated number (never the official support line), warm it up by normal use first,
> and keep replies sparse.

## How it decides

- Messages in a group are batched for `GROUP_AGENT_DEBOUNCE_SECONDS` (default 6), so a burst gets one decision.
- The LLM sees the last `GROUP_AGENT_HISTORY` messages (default 30) - earlier ones marked as context, the
  rest as NEW - plus the instructions in [group_agent/prompt.md](group_agent/prompt.md). It replies by calling
  the `send_reply` tool, or stays silent by calling nothing.
- At most one reply per decision, and at least `GROUP_AGENT_MIN_REPLY_GAP_SECONDS` (default 60) between
  replies in the same group.
- **Only names are sent to the LLM** - never phone numbers.
- If no model answers (main and fallback both down), it looks at the chat again every minute.
- It identifies itself as an AI assistant and never claims to be human (in the prompt).

## Safety switches

| Setting | Effect |
|---|---|
| `GROUP_AGENT_ENABLED=false` | Off entirely; the bridge is not started |
| `GROUP_AGENT_MODE=listen` | Reads and stores every message, no LLM, **never sends** |
| `GROUP_AGENT_MODE=observe` | Logs and tracks issues in every group (Group Issues sheet), **never sends** |
| `GROUP_AGENT_MODE=reply` + `GROUP_AGENT_REPLY_CHATS=Test group` | Full agent in the listed groups (names or ids; `*` = all); every other group is observed |
| `LLM_BASE_URL` empty | Test mode: replies only when someone writes "assistant", with a fixed message |

## Setup

1. **The phone:** a dedicated SIM with WhatsApp (or WhatsApp Business) installed and used normally for
   a week or two. Add it to the groups.
2. **Settings** (in Depot's environment variables):
   ```
   GROUP_AGENT_ENABLED=true
   GROUP_AGENT_MODE=listen
   BRIDGE_SECRET=<long random string>
   LLM_BASE_URL=https://api.your-provider.com/v1
   LLM_API_KEY=<key>
   LLM_MODEL=<model name>
   LLM_FALLBACK_API_KEY=<Gemini API key>   # optional, see "Fallback model" below
   ```
3. **Deploy**, then link the phone - either:
   - **Code:** `POST /api/v1/group-agent/pair` with `{"phone": "2348012345678"}` (header `X-API-Key`) returns an
     8-character code; on the phone: WhatsApp → Settings → Linked devices → Link a device →
     **Link with phone number instead** → enter the code.
   - **QR:** `GET /api/v1/group-agent/qr` (header `X-API-Key`) returns the QR image to scan from Linked devices.
4. Check `GET /api/v1/group-agent/status`: `bridge.connected` should be `true`, and `chats` fills up as messages arrive.
5. To test replies, create a small test group, then set `GROUP_AGENT_MODE=reply` and
   `GROUP_AGENT_REPLY_CHATS=<test group name>`.

**Staying linked:** with `DATABASE_URL` set (as on Depot), the linked-device session is stored in the
PostgreSQL table `wa_auth`, so the phone stays linked across redeploys. Treat that table as a password:
anyone with it can act as the WhatsApp number. If the phone removes the linked device, the session is cleared
automatically and a new pairing code or QR can be requested.

## Using a self-hosted vLLM model

vLLM serves an OpenAI-compatible API, so no code changes are needed:

```
LLM_BASE_URL=http://<vllm-host>:8000/v1   # "/v1" is added automatically if left off
LLM_MODEL=<the model name vLLM serves, as listed by GET /v1/models>
# LLM_API_KEY is not needed unless vLLM was started with --api-key
```

The agent decides when to reply through tool calling. vLLM supports that only when started with
`--enable-auto-tool-choice --tool-call-parser <parser>` (e.g. `hermes` for Qwen, `llama3_json` for Llama 3).
If it wasn't, nothing breaks: with `LLM_TOOL_MODE=auto` (default) the agent notices the refusal and switches to
asking the model for a JSON answer instead. `<think>` reasoning blocks are ignored.

The Depot server must be able to reach the vLLM address. Check it with
`POST /api/v1/group-agent/llm-check` (header `X-API-Key`): a dry run that sends one test conversation to the
model and shows the reply it would give - nothing is sent to WhatsApp.

## Fallback model (Gemini)

If the main model fails (for example the vLLM server is down), the agent answers with a second model, and
keeps using it for 5 minutes before trying the main one again. If both fail, it looks at the chat again every
minute until one answers, so no complaint is missed.

```
LLM_FALLBACK_API_KEY=<Gemini API key from https://aistudio.google.com/apikey>
# defaults: LLM_FALLBACK_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
#           LLM_FALLBACK_MODEL=gemini-flash-latest   (Google's current free tier Flash model)
```

Any OpenAI-compatible API works the same way. `POST /api/v1/group-agent/llm-check` tests both models.
On Gemini's free tier Google may use the messages sent to improve its products; only names and message text
are sent, never phone numbers. A paid (billing-enabled) Gemini key avoids this.

## Endpoints (all need `X-API-Key`)

| Endpoint | Returns |
|---|---|
| `GET /api/v1/group-agent/status` | Mode, LLM in use, bridge connection, message counts per group |
| `POST /api/v1/group-agent/pair` | Pairing code for "Link with phone number" |
| `GET /api/v1/group-agent/qr` | Login QR code (PNG) |
| `POST /api/v1/group-agent/llm-check` | Dry-run test of the LLM connection and its reply |
| `GET /api/v1/group-agent/groups` | Groups the number is in, with member counts |
| `GET /api/v1/group-agent/issues` | Issues with their status, timings and scores |
| `GET /api/v1/group-agent/messages?chat=<id>` | Stored messages, newest first |
