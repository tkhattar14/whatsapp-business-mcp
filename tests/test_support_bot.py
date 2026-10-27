"""
End-to-end tests for the support bot: signed webhooks in, mocked WhatsApp API out.

Run with:  python -m pytest tests
"""

import asyncio
import hashlib
import hmac
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
API_KEY = "test-api-key"
PHONE_ID = "1111111111"
CUSTOMER = "2348012345678"


AGENT = "2348000000001"


@pytest.fixture
def env(monkeypatch, tmp_path):
    values = {
        "META_ACCESS_TOKEN": "EAAtesttoken",
        "META_PHONE_NUMBER_ID": PHONE_ID,
        "WABA_ID": "2222222222",
        "META_APP_SECRET": APP_SECRET,
        "WEBHOOK_VERIFY_TOKEN": VERIFY_TOKEN,
        "MCP_API_KEY": API_KEY,
        "SUPPORT_DB_PATH": str(tmp_path / "support.db"),
        "LOG_TIMEZONE": "Africa/Lagos",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    # main_http loads the real .env on import - keep its optional settings out of the tests
    for key in ("SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET",
                "SHAREPOINT_HOSTNAME", "SHAREPOINT_SITE_PATH", "SHAREPOINT_FILE_PATH", "SHAREPOINT_FILE_URL",
                "SUPPORT_FLOW_FILE", "AGENT_WHATSAPP_NUMBER", "AGENT_ALERT_TEMPLATE", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)

    # Set TEST_DATABASE_URL to run the whole suite against PostgreSQL (tables are reset per test)
    test_db = os.environ.get("TEST_DATABASE_URL")
    if test_db:
        import psycopg
        from support_bot.store import TABLES
        with psycopg.connect(test_db, autocommit=True) as conn:
            for table in TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        monkeypatch.setenv("DATABASE_URL", test_db)
    return values


class Calls(list):
    """Recorded WhatsApp API calls, plus switches to simulate Meta's 24-hour-window error."""
    agent_window_closed = False
    resident_window_closed = False

    def to(self, number):
        return [c["data"] for c in self if (c["data"] or {}).get("to") == number]


WINDOW_ERROR = {
    "status": "error", "status_code": 400, "message": "API request failed with status 400",
    "error": {"error": {"code": 131047, "message": "Re-engagement message"}},
}


@pytest.fixture
def sent(monkeypatch):
    """Capture every WhatsApp API call instead of hitting graph.facebook.com."""
    from mcp_server.base_handler import BaseWhatsAppHandler

    calls = Calls()

    async def fake_request(self, method, url, data=None, params=None, files=None):
        calls.append({"method": method, "url": url, "data": data})
        if files:
            return {"status": "success", "data": {"id": f"media.uploaded{len(calls)}"}}
        to = (data or {}).get("to")
        is_template = (data or {}).get("type") == "template"
        if to == AGENT and calls.agent_window_closed and not is_template:
            return WINDOW_ERROR
        if to == CUSTOMER and calls.resident_window_closed:
            return WINDOW_ERROR
        return {"status": "success", "data": {"messages": [{"id": f"wamid.bot{len(calls)}"}]}}

    monkeypatch.setattr(BaseWhatsAppHandler, "_make_request", fake_request)
    return calls


@pytest.fixture
def client(env, sent):
    import main_http

    with TestClient(main_http.app) as test_client:
        yield test_client


def post_webhook(client, value, field="messages", secret=APP_SECRET):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "2222222222", "changes": [{"field": field, "value": value}]}],
    }).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={
        "X-Hub-Signature-256": signature, "Content-Type": "application/json",
    })


def customer_sends(client, wamid, message):
    message = {"from": CUSTOMER, "id": wamid, "timestamp": str(int(time.time())), **message}
    return post_webhook(client, {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": PHONE_ID},
        "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Ada Obi"}}],
        "messages": [message],
    })


def says(client, wamid, text):
    return customer_sends(client, wamid, {"type": "text", "text": {"body": text}})


def taps_button(client, wamid, option_id, title):
    return customer_sends(client, wamid, {"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": option_id, "title": title}}})


def picks_from_list(client, wamid, option_id, title):
    return customer_sends(client, wamid, {"type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": option_id, "title": title}}})


def agent_says(client, wamid, text):
    return post_webhook(client, {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": PHONE_ID},
        "message_echoes": [{
            "from": "2340000000000", "to": CUSTOMER, "id": wamid,
            "timestamp": str(int(time.time())), "type": "text", "text": {"body": text},
        }],
    }, field="smb_message_echoes")


def store(client):
    return client.app.state.support.store


def conversation(client):
    return store(client).get_conversation(CUSTOMER)


def last_sent_text(sent):
    data = sent[-1]["data"]
    if data["type"] == "text":
        return data["text"]["body"]
    return data["interactive"]["body"]["text"]


# ================================
# WEBHOOK SECURITY
# ================================

def test_webhook_verification(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"})
    assert bad.status_code == 403


def test_rejects_bad_signature(client, sent):
    response = post_webhook(client, {"messages": []}, secret="not-the-secret")
    assert response.status_code == 403
    assert sent == []


def test_ignores_other_phone_numbers(client, sent):
    post_webhook(client, {
        "metadata": {"phone_number_id": "9999999999"},
        "messages": [{"from": CUSTOMER, "id": "wamid.x", "timestamp": "1", "type": "text", "text": {"body": "hi"}}],
    })
    assert sent == []
    assert store(client).get_messages() == []


# ================================
# MAX MIGOLD INTAKE FLOW
# ================================

SERVICE_AREAS = ["Plumbing", "Cleaning", "Security", "HVAC", "Civil Work", "Waste Management",
                 "Landscaping", "Electrical", "Others"]


def start_conversation(client, sent):
    says(client, "wamid.s0", "Hello")
    return sent[-1]["data"]


def buttons(sent):
    return [b["reply"]["title"] for b in sent[-1]["data"]["interactive"]["action"]["buttons"]]


def list_rows(sent):
    return [r["title"] for r in sent[-1]["data"]["interactive"]["action"]["sections"][0]["rows"]]


def complete_ticket(client, sent, prefix="t"):
    """Seplat Eket -> complaint -> Plumbing -> description."""
    says(client, f"wamid.{prefix}1", "hi")
    says(client, f"wamid.{prefix}2", "Seplat Eket")
    says(client, f"wamid.{prefix}3", "complaint")
    says(client, f"wamid.{prefix}4", "Plumbing")
    says(client, f"wamid.{prefix}5", "Low water pressure")


def test_la_tour_complaint_end_to_end(client, sent):
    first = start_conversation(client, sent)
    body = first["interactive"]["body"]["text"]
    assert body.startswith("Hello Ada! 👋 Welcome to the *Max Migold Facility Management* support channel.")
    assert "Which location are you contacting us from?" in body
    assert buttons(sent) == ["La Tour", "Lagoon", "Seplat Eket"]

    taps_button(client, "wamid.c2", "la_tour", "La Tour")
    assert last_sent_text(sent) == "What is your flat number? (1 - 45)"

    says(client, "wamid.c3", "Flat 52")
    assert last_sent_text(sent).startswith("Please enter a valid flat number between 1 and 45.")
    says(client, "wamid.c4", "Flat 12")
    assert last_sent_text(sent) == "Would you like to lodge a complaint or give feedback on our service?"
    assert buttons(sent) == ["Lodge a complaint", "Give feedback"]

    taps_button(client, "wamid.c5", "complaint", "Lodge a complaint")
    assert last_sent_text(sent) == "Which service area is your complaint about?"
    assert list_rows(sent) == SERVICE_AREAS

    picks_from_list(client, "wamid.c6", "plumbing", "Plumbing")
    assert "describe your complaint" in last_sent_text(sent)

    says(client, "wamid.c7", "The kitchen sink has been leaking since yesterday.")
    done = last_sent_text(sent)
    assert "Your complaint has been logged and will be responded to as soon as possible." in done
    assert "Your ticket ID is *MMF-00001*." in done
    assert conversation(client)["state"] == "agent"

    ticket = store(client).get_ticket("MMF-00001")
    assert ticket["status"] == "Pending"
    assert ticket["answers"] == {
        "Location": "La Tour", "Flat Number": "12", "Request Type": "Complaint", "Service Area": "Plumbing",
        "Complaint": "The kitchen sink has been leaking since yesterday.",
    }


def test_lagoon_has_no_follow_up_question(client, sent):
    start_conversation(client, sent)
    taps_button(client, "wamid.l1", "lagoon", "Lagoon")
    assert buttons(sent) == ["Lodge a complaint", "Give feedback"]


def test_seplat_eket_feedback(client, sent):
    start_conversation(client, sent)
    says(client, "wamid.e1", "3")  # typed option number
    taps_button(client, "wamid.e2", "feedback", "Give feedback")
    assert last_sent_text(sent) == "Which service area is your feedback about?"
    says(client, "wamid.e3", "2")  # Cleaning
    assert last_sent_text(sent) == "Please share your feedback about our service."
    says(client, "wamid.e4", "The cleaners did a great job this week")
    done = last_sent_text(sent)
    assert "Your feedback has been received (reference *MMF-00001*)" in done
    answers = store(client).get_ticket("MMF-00001")["answers"]
    assert answers == {"Location": "Seplat Eket", "Request Type": "Feedback", "Service Area": "Cleaning",
                       "Feedback": "The cleaners did a great job this week"}
    assert "Complaint" not in answers


def test_photo_as_complaint(client, sent):
    start_conversation(client, sent)
    says(client, "wamid.p1", "Lagoon")
    says(client, "wamid.p2", "complaint")
    picks_from_list(client, "wamid.p3", "waste_management", "Waste Management")
    customer_sends(client, "wamid.p4", {"type": "image", "image": {"id": "media77", "caption": "bins overflowing"}})
    complaint = store(client).get_ticket("MMF-00001")["answers"]["Complaint"]
    assert "[image]" in complaint and "bins overflowing" in complaint and "media77" in complaint


def test_ac_keyword_selects_hvac(client, sent):
    start_conversation(client, sent)
    says(client, "wamid.h1", "Seplat Eket")
    says(client, "wamid.h2", "complaint")
    says(client, "wamid.h3", "AC")
    says(client, "wamid.h4", "Not cooling in the lobby")
    assert store(client).get_ticket("MMF-00001")["answers"]["Service Area"] == "HVAC"


def test_others_service_area(client, sent):
    start_conversation(client, sent)
    says(client, "wamid.o1", "Seplat Eket")
    says(client, "wamid.o2", "complaint")
    picks_from_list(client, "wamid.o3", "others", "Others")
    says(client, "wamid.o4", "Gym equipment is broken")
    assert store(client).get_ticket("MMF-00001")["answers"]["Service Area"] == "Others"


def test_invalid_choice_asks_again(client, sent):
    start_conversation(client, sent)
    says(client, "wamid.v1", "banana")
    text = last_sent_text(sent)
    assert text.startswith("Sorry, please choose")
    assert "Which location" in text
    assert conversation(client)["step"] == "location"


def test_restart_keyword(client, sent):
    start_conversation(client, sent)
    taps_button(client, "wamid.r1", "la_tour", "La Tour")
    says(client, "wamid.r2", "menu")
    assert "Welcome" in last_sent_text(sent)
    assert conversation(client)["step"] == "location"


def test_abandoned_intake_restarts(client, sent):
    start_conversation(client, sent)
    taps_button(client, "wamid.ab1", "la_tour", "La Tour")
    store(client).upsert_conversation(CUSTOMER, last_customer_at=int(time.time()) - 25 * 3600)
    says(client, "wamid.ab2", "hello again")
    assert "Welcome" in last_sent_text(sent)
    assert store(client).list_tickets() == []


def test_coexistence_agent_can_step_in_mid_intake(client, sent):
    start_conversation(client, sent)
    agent_says(client, "wamid.a0", "Hi Ada, I can see you - how can I help?")
    assert conversation(client)["state"] == "agent"
    before = len(sent)
    says(client, "wamid.m1", "my AC is not cooling")
    assert len(sent) == before


def test_bot_resumes_after_agent_idle(client, sent):
    complete_ticket(client, sent)
    long_ago = int(time.time()) - 13 * 3600
    store(client).upsert_conversation(CUSTOMER, handoff_at=long_ago, last_agent_at=long_ago)
    says(client, "wamid.i9", "hello again")
    assert conversation(client)["state"] == "bot"
    assert "Welcome" in last_sent_text(sent)


def test_duplicate_webhooks_are_ignored(client, sent):
    says(client, "wamid.d1", "hi")
    says(client, "wamid.d1", "hi")
    assert len(sent) == 1
    assert len(store(client).get_messages()) == 2  # one in, one out


def test_flow_validation(tmp_path):
    from support_bot.bot import load_flow

    bad = tmp_path / "flow.json"
    bad.write_text(json.dumps({
        "greeting": "Hi", "start": "a", "done": "Done",
        "steps": {"a": {"type": "text", "field": "A", "question": "Q?", "next": "missing"}},
    }))
    with pytest.raises(ValueError, match="unknown step 'missing'"):
        load_flow(str(bad))


# ================================
# TICKET STATUS CHECK
# ================================

def test_status_check_pending(client, sent):
    complete_ticket(client, sent)
    for i, text in enumerate(["MMF-00001", "mmf 1", "status MMF-00001"]):
        says(client, f"wamid.q{i}", text)
        reply = last_sent_text(sent)
        assert reply.startswith("Ticket *MMF-00001*\nStatus: *Pending*")
        assert "• Service Area: Plumbing" in reply


def test_status_check_reads_live_status_from_excel(client, sent):
    complete_ticket(client, sent)
    store(client).mark_tickets_synced([1])  # as if the sync loop had written it to Excel

    async def excel_status(ref):
        assert ref == "MMF-00001"
        return "In Progress"

    client.app.state.support.bot.status_reader = excel_status
    says(client, "wamid.q1", "MMF-00001")
    assert "Status: *In Progress*" in last_sent_text(sent)
    assert store(client).get_ticket("MMF-00001")["status"] == "In Progress"


@pytest.mark.parametrize("typed_in_excel,shown", [
    ("inprogress", "In Progress"), ("IN PROGRESS", "In Progress"), ("in-progress", "In Progress"),
    ("resolved ", "Resolved"), ("Pending", "Pending"),
])
def test_status_from_excel_is_tidied(client, sent, typed_in_excel, shown):
    complete_ticket(client, sent)
    store(client).mark_tickets_synced([1])

    async def excel_status(ref):
        return typed_in_excel

    client.app.state.support.bot.status_reader = excel_status
    says(client, "wamid.q1", "MMF-00001")
    assert f"Status: *{shown}*" in last_sent_text(sent)


def test_status_check_falls_back_when_excel_unreachable(client, sent):
    complete_ticket(client, sent)
    store(client).mark_tickets_synced([1])

    async def broken(ref):
        raise RuntimeError("SharePoint down")

    client.app.state.support.bot.status_reader = broken
    says(client, "wamid.q1", "MMF-00001")
    assert "Status: *Pending*" in last_sent_text(sent)


def test_status_check_only_for_own_tickets(client, sent):
    complete_ticket(client, sent)
    post_webhook(client, {
        "metadata": {"phone_number_id": PHONE_ID},
        "contacts": [{"wa_id": "2349099999999", "profile": {"name": "Eve"}}],
        "messages": [{"from": "2349099999999", "id": "wamid.eve", "timestamp": str(int(time.time())),
                      "type": "text", "text": {"body": "MMF-00001"}}],
    })
    assert "couldn't find ticket *MMF-00001*" in last_sent_text(sent)
    assert "Low water pressure" not in last_sent_text(sent)


def test_status_check_does_not_disturb_intake(client, sent):
    complete_ticket(client, sent)
    long_ago = int(time.time()) - 13 * 3600
    store(client).upsert_conversation(CUSTOMER, handoff_at=long_ago, last_agent_at=long_ago)
    says(client, "wamid.n1", "hi")  # starts a new request
    taps_button(client, "wamid.n2", "la_tour", "La Tour")
    says(client, "wamid.n3", "MMF-00001")  # checks the old one mid-way
    assert "Status: *Pending*" in last_sent_text(sent)
    assert conversation(client)["step"] == "flat_number"


# ================================
# RELAY MODE (agent replies from their own WhatsApp)
# ================================

@pytest.fixture
def relay(env, sent, monkeypatch):
    import main_http

    monkeypatch.setenv("AGENT_WHATSAPP_NUMBER", "+234 800 000 0001")
    monkeypatch.setenv("AGENT_ALERT_TEMPLATE", "agent_new_messages")
    with TestClient(main_http.app) as test_client:
        yield test_client


def agent_sends(client, wamid, text=None, reply_to=None, image=None):
    message = {"from": AGENT, "id": wamid, "timestamp": str(int(time.time()))}
    if image:
        message.update({"type": "image", "image": image})
    else:
        message.update({"type": "text", "text": {"body": text}})
    if reply_to:
        message["context"] = {"from": "2340000000000", "id": reply_to}
    return post_webhook(client, {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": PHONE_ID},
        "contacts": [{"wa_id": AGENT, "profile": {"name": "Agent Bola"}}],
        "messages": [message],
    })


def forward_ids(sent):
    """WhatsApp IDs of the messages forwarded to the agent (fake IDs are wamid.bot<call number>)."""
    return [f"wamid.bot{i + 1}" for i, c in enumerate(sent) if (c["data"] or {}).get("to") == AGENT]


def test_relay_forwards_new_ticket_to_agent(relay, sent):
    complete_ticket(relay, sent)
    forwards = sent.to(AGENT)
    assert len(forwards) == 1
    text = forwards[0]["text"]["body"]
    assert text.startswith("🆕 *New complaint MMF-00001*")
    assert f"+{CUSTOMER}" in text
    assert "• Complaint: Low water pressure" in text
    assert "Swipe-reply" in text


def test_agent_swipe_reply_reaches_resident(relay, sent):
    complete_ticket(relay, sent)
    forward_id = forward_ids(sent)[0]
    before = len(sent.to(CUSTOMER))

    agent_sends(relay, "wamid.ag1", "Hello Ada, a plumber will come at 2pm.", reply_to=forward_id)

    to_resident = sent.to(CUSTOMER)
    assert len(to_resident) == before + 1
    assert to_resident[-1]["text"]["body"] == "Hello Ada, a plumber will come at 2pm."
    reaction = sent.to(AGENT)[-1]
    assert reaction["type"] == "reaction" and reaction["reaction"] == {"message_id": "wamid.ag1", "emoji": "✅"}

    logged = store(relay).get_messages(CUSTOMER)[-1]
    assert (logged["direction"], logged["sender"], logged["body"]) == ("out", "agent", "Hello Ada, a plumber will come at 2pm.")
    assert conversation(relay)["last_agent_at"]


def test_agent_reply_using_ticket_id(relay, sent):
    complete_ticket(relay, sent)
    agent_sends(relay, "wamid.ag1", "MMF-00001 we'll be there at 2pm")
    assert sent.to(CUSTOMER)[-1]["text"]["body"] == "we'll be there at 2pm"


def test_resident_follow_up_is_forwarded(relay, sent):
    complete_ticket(relay, sent)
    says(relay, "wamid.f1", "Any update please?")
    assert sent.to(AGENT)[-1]["text"]["body"] == "💬 *MMF-00001* · Ada Obi:\nAny update please?"
    # ...and the agent can swipe-reply to the follow-up too
    agent_sends(relay, "wamid.ag2", "Coming now", reply_to=forward_ids(sent)[-1])
    assert sent.to(CUSTOMER)[-1]["text"]["body"] == "Coming now"


def test_agent_messages_never_enter_questionnaire(relay, sent):
    agent_sends(relay, "wamid.ag1", "hi")
    assert sent == []  # a short "hi" just opens the agent's window
    agent_sends(relay, "wamid.ag2", "What is happening with the leak in flat 12?")
    assert "swipe-reply" in sent.to(AGENT)[-1]["text"]["body"]
    assert store(relay).get_conversation(AGENT) is None


def test_agent_webhook_retry_sends_once(relay, sent):
    complete_ticket(relay, sent)
    forward_id = forward_ids(sent)[0]
    agent_sends(relay, "wamid.ag1", "On our way", reply_to=forward_id)
    agent_sends(relay, "wamid.ag1", "On our way", reply_to=forward_id)
    assert [m.get("text", {}).get("body") for m in sent.to(CUSTOMER)].count("On our way") == 1


def test_forwards_queue_while_agent_window_closed(relay, sent):
    sent.agent_window_closed = True
    complete_ticket(relay, sent)
    says(relay, "wamid.f1", "Hello?")
    queued = store(relay).count_relay_queue()
    assert queued == 2  # the new ticket + the follow-up
    templates = [d for d in sent.to(AGENT) if d["type"] == "template"]
    assert len(templates) == 1  # alerted once, not per message
    assert templates[0]["template"]["name"] == "agent_new_messages"
    params = templates[0]["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["complaint", "Seplat Eket"]

    # Agent messages the business number -> window opens -> queued forwards are delivered
    sent.agent_window_closed = False
    agent_sends(relay, "wamid.ag1", "hi")
    delivered = [d["text"]["body"] for d in sent.to(AGENT)[-2:]]
    assert delivered[0].startswith("🆕 *New complaint MMF-00001*")
    assert delivered[1] == "💬 *MMF-00001* · Ada Obi:\nHello?"
    assert store(relay).take_relay_queue() == []


def test_agent_told_when_resident_window_closed(relay, sent):
    complete_ticket(relay, sent)
    sent.resident_window_closed = True
    agent_sends(relay, "wamid.ag1", "Sorry for the delay", reply_to=forward_ids(sent)[0])
    assert "❌ Not delivered to MMF-00001" in sent.to(AGENT)[-1]["text"]["body"]
    assert "24 hours" in sent.to(AGENT)[-1]["text"]["body"]


def test_photos_are_relayed_both_ways(relay, sent, monkeypatch):
    from support_bot.relay import AgentRelay

    async def fake_rehost(self, media_id, filename):
        return f"rehosted-{media_id}"

    monkeypatch.setattr(AgentRelay, "_rehost_media", fake_rehost)

    # Resident's complaint is a photo -> agent gets the summary and the photo
    says(relay, "wamid.p1", "hi")
    says(relay, "wamid.p2", "Lagoon")
    says(relay, "wamid.p3", "complaint")
    says(relay, "wamid.p4", "Plumbing")
    customer_sends(relay, "wamid.p5", {"type": "image", "image": {"id": "res-photo", "caption": "leak"}})
    photo = sent.to(AGENT)[-1]
    assert photo["type"] == "image" and photo["image"]["id"] == "rehosted-res-photo"
    assert photo["image"]["caption"] == "💬 *MMF-00001* · Ada Obi:\nleak"

    # Agent swipe-replies with a photo -> resident gets it
    agent_sends(relay, "wamid.ag1", reply_to=forward_ids(sent)[-1],
                image={"id": "agent-photo", "caption": "Fixed!"})
    to_resident = sent.to(CUSTOMER)[-1]
    assert to_resident["type"] == "image" and to_resident["image"] == {"id": "rehosted-agent-photo", "caption": "Fixed!"}


# ================================
# EXPORT
# ================================

def test_export_requires_api_key(client):
    assert client.get("/api/v1/support/export.csv").status_code == 401


def test_export_csv_escapes_formulas(client):
    says(client, "wamid.f1", '=HYPERLINK("http://evil","click")')
    response = client.get("/api/v1/support/export.csv", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    text = response.text
    assert text.splitlines()[0].lstrip("\ufeff").startswith("Date/Time,Phone,Customer Name")
    assert "'=HYPERLINK" in text
    assert "'+" + CUSTOMER in text


def test_tickets_csv(client, sent):
    complete_ticket(client, sent)
    response = client.get("/api/v1/support/tickets.csv", headers={"X-API-Key": API_KEY})
    lines = response.text.lstrip("\ufeff").splitlines()
    assert lines[0] == ("Ticket #,Date/Time,Phone,Customer Name,Location,Flat Number,"
                        "Request Type,Service Area,Complaint,Feedback,Status")
    assert lines[1].startswith("MMF-00001,")
    assert lines[1].endswith("Ada Obi,Seplat Eket,,Complaint,Plumbing,Low water pressure,,Pending")


# ================================
# SHAREPOINT
# ================================

@pytest.fixture
def graph(env, monkeypatch):
    """Fake Microsoft Graph: records requests; the Tickets table content can be set per test."""
    from support_bot import sharepoint

    for key, value in {
        "SHAREPOINT_TENANT_ID": "tenant", "SHAREPOINT_CLIENT_ID": "client", "SHAREPOINT_CLIENT_SECRET": "secret",
        "SHAREPOINT_HOSTNAME": "contoso.sharepoint.com", "SHAREPOINT_SITE_PATH": "/sites/Support",
        "SHAREPOINT_FILE_PATH": "WhatsApp/Complaints Log.xlsx",
    }.items():
        monkeypatch.setenv(key, value)

    state = {"requests": [], "ticket_columns": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        url = str(request.url)
        if "login.microsoftonline.com" in url:
            return httpx.Response(200, json={"access_token": "graph-token", "expires_in": 3600})
        if url.endswith("/sites/contoso.sharepoint.com:/sites/Support"):
            return httpx.Response(200, json={"id": "site-1"})
        if "/drive/root:/" in url:
            assert "Complaints%20Log.xlsx" in url
            return httpx.Response(200, json={"id": "item-1"})
        if "/shares/u!" in url and url.endswith("/driveItem"):
            return httpx.Response(200, json={"id": "item-9", "parentReference": {"driveId": "drive-7"}})
        if url.endswith("/workbook/tables/Conversations/rows") or url.endswith("/workbook/tables/Tickets/rows"):
            return httpx.Response(201, json={})
        if url.endswith("/workbook/tables/Tickets/columns"):
            return httpx.Response(200, json={"value": state["ticket_columns"]})
        return httpx.Response(404, json={"error": {"message": "unexpected " + url}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(sharepoint.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return state


def test_sharepoint_sync(graph, tmp_path):
    from support_bot import sharepoint
    from support_bot.config import SupportSettings
    from support_bot.store import SupportStore

    settings = SupportSettings.from_env()
    assert settings.sharepoint_configured
    store = SupportStore(str(tmp_path / "sync.db"))
    store.add_message(CUSTOMER, "in", "customer", "text", "Hello", ts=1700000000, wamid="w1", name="Ada", state="bot")
    store.add_message(CUSTOMER, "out", "bot", "text", "Hi!", ts=1700000001, wamid="w2", name="Ada", state="bot")
    store.create_ticket(CUSTOMER, "Ada", {"Location": "La Tour", "Complaint": "=1+1"}, "MMF", "Pending")

    logger = sharepoint.SharePointExcelLogger(settings, store, ["Location", "Flat Number", "Complaint"])

    assert asyncio.run(logger.sync_once()) == 2
    assert store.get_unsynced() == []
    requests = graph["requests"]
    rows = json.loads(requests[-1].content)["values"]
    assert requests[-1].headers["Authorization"] == "Bearer graph-token"
    assert rows[0] == ["2023-11-14 23:13:20", "'+" + CUSTOMER, "Ada", "Incoming", "Customer",
                       "text", "Hello", "bot", "w1"]
    assert rows[1][3:5] == ["Outgoing", "Bot"]

    assert asyncio.run(logger.sync_tickets_once()) == 1
    assert str(requests[-1].url).endswith("/tables/Tickets/rows")
    ticket_row = json.loads(requests[-1].content)["values"][0]
    assert ticket_row[0] == "MMF-00001"
    assert ticket_row[2:] == ["'+" + CUSTOMER, "Ada", "La Tour", "", "'=1+1", "Pending"]
    assert store.list_tickets(unsynced_only=True) == []
    store.close()


def test_sharepoint_file_from_sharing_link(graph, tmp_path, monkeypatch):
    import base64
    from support_bot import sharepoint
    from support_bot.config import SupportSettings
    from support_bot.store import SupportStore

    link = "https://contoso.sharepoint.com/:x:/s/Support/IQAbCdEf123?e=xYz789"
    for key in ("SHAREPOINT_HOSTNAME", "SHAREPOINT_SITE_PATH", "SHAREPOINT_FILE_PATH"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("SHAREPOINT_FILE_URL", link)

    settings = SupportSettings.from_env()
    assert settings.sharepoint_configured
    store = SupportStore(str(tmp_path / "link.db"))
    store.add_message(CUSTOMER, "in", "customer", "text", "Hello", wamid="w1")
    assert asyncio.run(sharepoint.SharePointExcelLogger(settings, store, []).sync_once()) == 1

    share_request = next(r for r in graph["requests"] if "/shares/" in str(r.url))
    token = str(share_request.url).split("/shares/u!")[1].split("/")[0]
    assert base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode() == link
    assert str(graph["requests"][-1].url).endswith("/drives/drive-7/items/item-9/workbook/tables/Conversations/rows")
    store.close()


def test_sharepoint_reads_status(graph, tmp_path):
    from support_bot import sharepoint
    from support_bot.config import SupportSettings
    from support_bot.store import SupportStore

    graph["ticket_columns"] = [
        {"name": "Ticket #", "values": [["Ticket #"], ["MMF-00002"], ["MMF-00001"]]},
        {"name": "Location", "values": [["Location"], ["Lagoon"], ["La Tour"]]},
        {"name": "Status", "values": [["Status"], ["Resolved"], ["In Progress"]]},
    ]
    store = SupportStore(str(tmp_path / "status.db"))
    logger = sharepoint.SharePointExcelLogger(SupportSettings.from_env(), store, [])
    assert asyncio.run(logger.get_ticket_status("MMF-00001")) == "In Progress"
    assert asyncio.run(logger.get_ticket_status("MMF-00002")) == "Resolved"
    assert asyncio.run(logger.get_ticket_status("MMF-00009")) is None
    store.close()


def test_existing_database_is_migrated(tmp_path):
    import sqlite3
    from support_bot.store import SupportStore

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE conversations (phone TEXT PRIMARY KEY, name TEXT, state TEXT NOT NULL DEFAULT 'bot',
                    handoff_at INTEGER, last_customer_at INTEGER, last_agent_at INTEGER, updated_at INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, name TEXT,
                    answers TEXT NOT NULL, created_at INTEGER NOT NULL, synced INTEGER NOT NULL DEFAULT 0)""")
    conn.commit()
    conn.close()

    store = SupportStore(str(path))
    assert store.upsert_conversation(CUSTOMER, step="location")["step"] == "location"
    assert store.create_ticket(CUSTOMER, "Ada", {}, "MMF", "Pending")["ref"] == "MMF-00001"
    store.close()


def test_excel_template_matches_bot(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    import importlib.util
    from support_bot.bot import flow_fields, load_flow
    from support_bot.sharepoint import EXCEL_COLUMNS, ticket_columns

    spec = importlib.util.spec_from_file_location("create_excel_log", "scripts/create_excel_log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    output = tmp_path / "log.xlsx"
    module.build(str(output), "support_bot/flow.json")
    workbook = openpyxl.load_workbook(output)
    tickets_headers = [c.value for c in workbook["Tickets"][1]]
    conversation_headers = [c.value for c in workbook["Conversations"][1]]
    assert tickets_headers == ticket_columns(flow_fields(load_flow("support_bot/flow.json")))
    assert conversation_headers == EXCEL_COLUMNS
    assert "Tickets" in workbook["Tickets"].tables and "Conversations" in workbook["Conversations"].tables


def test_late_delivery_failure_is_queued(relay, sent):
    """Meta accepts the forward, then reports 131047 in a status webhook - it must be queued, not lost."""
    complete_ticket(relay, sent)
    forward_id = forward_ids(sent)[0]
    post_webhook(relay, {
        "metadata": {"phone_number_id": PHONE_ID},
        "statuses": [{"id": forward_id, "status": "failed", "recipient_id": AGENT,
                      "errors": [{"code": 131047, "title": "Re-engagement message"}]}],
    })
    templates = [d for d in sent.to(AGENT) if d["type"] == "template"]
    assert len(templates) == 1
    assert [p["text"] for p in templates[0]["template"]["components"][0]["parameters"]] == ["complaint", "Seplat Eket"]

    agent_sends(relay, "wamid.ag1", "hi")
    assert sent.to(AGENT)[-1]["text"]["body"].startswith("🆕 *New complaint MMF-00001*")
    assert store(relay).take_relay_queue() == []


def test_feedback_has_no_status(client, sent):
    start_conversation(client, sent)
    says(client, "wamid.fb1", "Lagoon")
    says(client, "wamid.fb2", "feedback")
    says(client, "wamid.fb3", "Security")
    says(client, "wamid.fb4", "The guards are very polite")
    assert store(client).get_ticket("MMF-00001")["status"] is None

    # Checking the reference gives a thank-you, not a status
    says(client, "wamid.fb5", "MMF-00001")
    reply = last_sent_text(sent)
    assert reply.startswith("Thank you — your feedback *MMF-00001* was received.")
    assert "Status" not in reply

    # Excel/CSV Status cell is left empty
    lines = client.get("/api/v1/support/tickets.csv", headers={"X-API-Key": API_KEY}).text.splitlines()
    assert lines[1].endswith(",The guards are very polite,")


def test_feedback_shows_status_if_agent_sets_one(client, sent):
    start_conversation(client, sent)
    says(client, "wamid.fs1", "Lagoon")
    says(client, "wamid.fs2", "feedback")
    says(client, "wamid.fs3", "Cleaning")
    says(client, "wamid.fs4", "Stairwell not cleaned")
    store(client).mark_tickets_synced([1])

    async def excel_status(ref):
        return "resolved"

    client.app.state.support.bot.status_reader = excel_status
    says(client, "wamid.fs5", "MMF-00001")
    assert "Status: *Resolved*" in last_sent_text(sent)


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to test PostgreSQL")
def test_copy_sqlite_to_postgres(env, tmp_path):
    import subprocess
    import sys
    from support_bot.store import SupportStore

    sqlite_path = str(tmp_path / "old.db")
    old = SupportStore(sqlite_path)
    old.create_ticket(CUSTOMER, "Ada", {"Location": "Lagoon"}, "MMF", "Pending")
    old.create_ticket(CUSTOMER, "Ada", {"Location": "La Tour"}, "MMF", None)
    old.add_message(CUSTOMER, "in", "customer", "text", "Hello", wamid="w1")
    old.add_relay_link("wamid.fwd", CUSTOMER, "MMF-00001", {"kind": "text"})
    old.close()

    url = os.environ["TEST_DATABASE_URL"]
    result = subprocess.run(
        [sys.executable, "scripts/copy_sqlite_to_postgres.py", sqlite_path],
        env={**os.environ, "MIGRATE_TO_DATABASE_URL": url, "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "MISMATCH" not in result.stdout

    new = SupportStore(url)
    assert new.get_ticket("MMF-00002")["answers"] == {"Location": "La Tour"}
    assert new.get_relay_link("wamid.fwd")["phone"] == CUSTOMER
    assert new.create_ticket(CUSTOMER, "Ada", {}, "MMF", "Pending")["ref"] == "MMF-00003"
    new.close()
