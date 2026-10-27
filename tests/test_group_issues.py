"""
Issue workflow of the group agent: report -> acknowledge -> staff response -> done ->
rating -> closed, plus reminders, escalation, observe mode, staff list and Excel sync.
The LLM is scripted and WhatsApp is faked - nothing is sent anywhere.
"""

import asyncio
import json
import time

import httpx
import pytest

GROUP = "120363000000000009@g.us"
NAME = "La Tour Flat 12"
ADA = "2348012345678@s.whatsapp.net"
TUNDE = "2348031111111@s.whatsapp.net"   # staff, first contact
BOLA = "2348032222222@s.whatsapp.net"    # staff, first contact
CHIKA = "2348033333333@s.whatsapp.net"   # staff, escalate to


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for key, value in {
        "GROUP_AGENT_MODE": "reply", "GROUP_AGENT_REPLY_CHATS": NAME, "BRIDGE_SECRET": "s",
        "GROUP_AGENT_DEBOUNCE_SECONDS": "0", "GROUP_AGENT_MIN_REPLY_GAP_SECONDS": "0",
        "GROUP_REMINDER_MINUTES": "30", "GROUP_ESCALATE_MINUTES": "60", "GROUP_FEEDBACK_HOURS": "24",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("DATABASE_URL", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SUPPORT_DB_PATH", str(tmp_path / "g.db"))
    from group_agent.config import GroupAgentSettings
    return GroupAgentSettings.from_env()


class FakeBridge:
    def __init__(self):
        self.sent = []

    async def send(self, chat, text, quoted=None, mentions=None):
        self.sent.append({"chat": chat, "text": text, "quoted": quoted, "mentions": mentions or []})
        return f"OUT{len(self.sent)}"


class ScriptedLLM:
    """Returns the tool calls the test queues up, one round at a time; silence afterwards."""
    name = "scripted"

    def __init__(self):
        self.rounds = []
        self.prompts = []

    def then(self, *calls):
        self.rounds.append([{"id": f"c{i}", "type": "function",
                             "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)])
        return self

    async def complete(self, messages, tools):
        if not any(m["role"] == "tool" for m in messages):
            self.prompts.append(messages[-1]["content"])
        calls = self.rounds.pop(0) if self.rounds else []
        return {"content": None, "tool_calls": calls}


@pytest.fixture
def staff():
    from group_agent.staff import ESCALATE_TO, FIRST_CONTACT, OBSERVER, StaffDirectory, StaffMember
    return StaffDirectory([
        StaffMember("Tunde Bakare", "2348031111111", "Technician", FIRST_CONTACT),
        StaffMember("Bola Ade", "2348032222222", "Facility Officer", FIRST_CONTACT),
        StaffMember("Chika Obi", "2348033333333", "Supervisor", ESCALATE_TO),
        StaffMember("The CEO", "2348034444444", "CEO", OBSERVER),
    ])


@pytest.fixture
def world(settings, staff, tmp_path):
    from group_agent.agent import GroupAgent
    from group_agent.store import GroupStore

    store = GroupStore(str(tmp_path / "issues.db"))
    bridge, llm = FakeBridge(), ScriptedLLM()
    agent = GroupAgent(settings, store, bridge, llm=llm, staff=staff)
    yield agent, store, bridge, llm
    store.close()


def msg(agent, msg_id, text, sender=ADA, name="Ada Obi", quoted=None, chat=GROUP, chat_name=NAME, ts=None,
        mentions=None):
    asyncio.run(agent.handle_event({
        "id": msg_id, "chat": chat, "chat_name": chat_name, "is_group": True, "sender": sender,
        "sender_name": name, "from_me": False, "type": "conversation", "text": text,
        "quoted_id": quoted, "mentions": mentions or [], "timestamp": ts or int(time.time()),
    }))


def report(agent, llm, msg_id="R1", text="No water in my flat since 6am"):
    llm.then(("log_issue", {"message_id": msg_id, "summary": "No water since 6am", "service_area": "Plumbing"})) \
       .then(("send_reply", {"text": "Sorry Ada, logged as MMF-G00001. The team will attend to it.", "reply_to": msg_id}))
    msg(agent, msg_id, text)


# ================================
# THE FULL LIFE OF AN ISSUE
# ================================

def test_issue_from_report_to_rating(world):
    agent, store, bridge, llm = world

    # 1. Resident reports -> logged + acknowledged in the group
    report(agent, llm)
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "Open" and issue["resident_name"] == "Ada Obi" and issue["service_area"] == "Plumbing"
    assert issue["acknowledged_at"] and issue["reported_msg_id"] == "R1"
    assert bridge.sent[-1]["chat"] == GROUP and bridge.sent[-1]["quoted"] == "R1"
    assert "MMF-G00001" in llm.prompts[-1] or "No water" in llm.prompts[-1]

    # 2. Staff responds -> in progress
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "in_progress", "note": "on my way"}))
    msg(agent, "S1", "I'm on my way to check", sender=TUNDE, name="Tunde")
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "In Progress" and issue["staff_name"] == "Tunde Bakare" and issue["staff_response_at"]
    assert "Tunde Bakare [STAFF, Technician]" in llm.prompts[-1]
    assert "In progress" in agent._issue_line(issue)

    # 3. Staff says done -> survey asked in the group, to the resident
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "resolved", "note": "pump fixed"}))
    msg(agent, "S2", "Done, the pump is fixed", sender=TUNDE, name="Tunde")
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "Awaiting Feedback" and issue["resolved_by"] == "Tunde Bakare" and issue["survey_sent_at"]
    survey = bridge.sent[-1]
    assert survey["chat"] == GROUP and survey["mentions"] == ["2348012345678"] and survey["quoted"] == "R1"
    assert survey["text"].startswith("@2348012345678") and "0 to 10" in survey["text"]

    # 4. Resident rates -> closed with the score and comment
    llm.then(("record_feedback", {"ref": "MMF-G00001", "score": 9, "comment": "Very quick"})) \
       .then(("send_reply", {"text": "Thank you, Ada!", "reply_to": "R2"}))
    msg(agent, "R2", "9, very quick")
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "Closed" and issue["score"] == 9 and issue["feedback"] == "Very quick" and issue["closed_at"]
    assert store.active_issues(GROUP) == []


def test_bare_number_rating_is_recorded_even_if_llm_misses_it(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "resolved"}))
    msg(agent, "S1", "Fixed", sender=TUNDE, name="Tunde")
    msg(agent, "R2", "8/10")  # LLM stays silent
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "Closed" and issue["score"] == 8


@pytest.mark.parametrize("text,score", [("9", 9), ("10", 10), ("8/10", 8), ("7 out of 10", 7), ("I give 6", 6),
                                        ("0", 0), ("no water again", None), ("Flat 12", None), ("11", None)])
def test_bare_score_parser(text, score):
    from group_agent.agent import _bare_score
    assert _bare_score(text) == score


# ================================
# SAFEGUARDS
# ================================

def test_only_staff_can_resolve(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "resolved"}))
    msg(agent, "R2", "Never mind, it's working now")  # resident, not staff
    assert store.get_issue("MMF-G00001")["status"] == "Open"


def test_staff_messages_are_not_logged_as_issues(world):
    agent, store, bridge, llm = world
    llm.then(("log_issue", {"message_id": "S1", "summary": "Generator service today", "service_area": "Electrical"}))
    msg(agent, "S1", "Generator servicing today at 2pm", sender=BOLA, name="Bola")
    assert store.list_issues() == []


def test_same_message_is_not_logged_twice(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    asyncio.run(agent._run_tool({"function": {"name": "log_issue", "arguments": json.dumps(
        {"message_id": "R1", "summary": "dup", "service_area": "Plumbing"})}},
        {"chat": GROUP, "chat_name": NAME, "new": {"R1": store.recent(GROUP, 1)[0] | {"msg_id": "R1"}},
         "created": [], "handled": set(), "replied": False, "sent": False, "can_send": True}))
    assert len(store.list_issues()) == 1


def test_staff_quoting_the_report_counts_as_a_response(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    msg(agent, "S1", "Checking now", sender=BOLA, name="Bola", quoted="R1")  # LLM silent
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "In Progress" and issue["staff_name"] == "Bola Ade"


def test_phone_numbers_never_reach_the_llm(world):
    agent, store, bridge, llm = world
    msg(agent, "R1", "Please call me on @2348099998888")
    prompt = llm.prompts[-1]
    assert "2348099998888" not in prompt and "2348012345678" not in prompt and "@someone" in prompt


# ================================
# TIMERS
# ================================

def test_reminder_then_escalation_then_nothing_more(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    opened = store.get_issue("MMF-G00001")["opened_at"]
    sent_before = len(bridge.sent)

    asyncio.run(agent.run_timers(now=opened + 29 * 60))
    assert len(bridge.sent) == sent_before  # not yet

    asyncio.run(agent.run_timers(now=opened + 31 * 60))
    reminder = bridge.sent[-1]
    assert reminder["chat"] == GROUP and set(reminder["mentions"]) == {"2348031111111", "2348032222222"}
    assert "@2348031111111" in reminder["text"] and "MMF-G00001" in reminder["text"] and "31 minutes" in reminder["text"]
    assert store.get_issue("MMF-G00001")["reminders"] == 1

    asyncio.run(agent.run_timers(now=opened + 45 * 60))
    assert len(bridge.sent) == sent_before + 1  # one reminder only

    asyncio.run(agent.run_timers(now=opened + 61 * 60))
    escalation = bridge.sent[-1]
    assert escalation["chat"] == "2348033333333@s.whatsapp.net"  # privately, to the supervisor
    assert "Escalation" in escalation["text"] and "La Tour Flat 12" in escalation["text"]
    assert all(s["chat"] != "2348034444444@s.whatsapp.net" for s in bridge.sent)  # CEO is never messaged

    asyncio.run(agent.run_timers(now=opened + 5 * 3600))
    assert len(bridge.sent) == sent_before + 2  # escalated once only


def test_no_reminder_once_staff_responded(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "in_progress"}))
    msg(agent, "S1", "On it", sender=TUNDE, name="Tunde")
    opened = store.get_issue("MMF-G00001")["opened_at"]
    count = len(bridge.sent)
    asyncio.run(agent.run_timers(now=opened + 3 * 3600))
    assert len(bridge.sent) == count


def test_unanswered_survey_closes_after_24_hours(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "resolved"}))
    msg(agent, "S1", "Done", sender=TUNDE, name="Tunde")
    asked = store.get_issue("MMF-G00001")["survey_sent_at"]
    asyncio.run(agent.run_timers(now=asked + 23 * 3600))
    assert store.get_issue("MMF-G00001")["status"] == "Awaiting Feedback"
    asyncio.run(agent.run_timers(now=asked + 25 * 3600))
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "Closed - No Feedback" and issue["score"] is None


# ================================
# OBSERVE MODE / GROUPS NOT ON THE ALLOW-LIST
# ================================

def test_other_groups_are_observed_not_spoken_in(world):
    agent, store, bridge, llm = world
    other = "120363000000000010@g.us"
    llm.then(("log_issue", {"message_id": "X1", "summary": "Gate broken", "service_area": "Security"})) \
       .then(("send_reply", {"text": "Logged!", "reply_to": "X1"}))
    msg(agent, "X1", "The gate is broken", chat=other, chat_name="Lagoon Building 2")
    issue = store.list_issues()[0]
    assert issue["chat_name"] == "Lagoon Building 2" and issue["status"] == "Open"
    assert issue["acknowledged_at"] is None  # nothing was actually sent
    assert bridge.sent == []
    asyncio.run(agent.run_timers(now=issue["opened_at"] + 3 * 3600))
    assert bridge.sent == []  # no reminders or escalations from an observed group
    assert "observing this group" in llm.prompts[-1]


# ================================
# STAFF LIST
# ================================

@pytest.mark.parametrize("value,expected", [
    ("0803 123 4567", "2348031234567"), ("+234 803 123 4567", "2348031234567"), ("2348031234567", "2348031234567"),
    (2348031234567, "2348031234567"), ("2348031234567.0", "2348031234567"), ("002348031234567", "2348031234567"),
])
def test_normalise_phone(value, expected):
    from group_agent.staff import normalise_phone
    assert normalise_phone(value) == expected


def test_staff_lookup(staff):
    assert staff.find("2348031111111:7@s.whatsapp.net").name == "Tunde Bakare"
    assert staff.find(ADA) is None
    assert staff.find("99887766@lid", "Bola Ade").name == "Bola Ade"  # hidden number -> name fallback
    assert staff.find("99887766@lid", "Someone Else") is None


def test_staff_loaded_from_sharepoint_table(monkeypatch):
    from group_agent import staff as staff_module
    from group_agent.staff import FIRST_CONTACT, OBSERVER, SharePointStaffSource, StaffDirectory

    class Connection:
        async def _get_token(self, client):
            return "t"

        async def _get_workbook_url(self, client, headers):
            return "https://graph/workbook"

    def handler(request):
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json={"value": [{"name": n} for n in
                                  ["Name", "WhatsApp Number", "Role", "Escalation", "Notes"]]})
        return httpx.Response(200, json={"value": [
            {"values": [["Tunde Bakare", "0803 111 1111", "Technician", "First contact", ""]]},
            {"values": [["The CEO", 2348034444444, "CEO", "Observer", ""]]},
            {"values": [["", "", "", "", ""]]},  # the empty starter row
        ]})

    real = httpx.AsyncClient
    monkeypatch.setattr(staff_module.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    directory = StaffDirectory()
    asyncio.run(SharePointStaffSource(Connection()).refresh(directory))
    assert [(m.name, m.phone, m.escalation) for m in directory.members] == [
        ("Tunde Bakare", "2348031111111", FIRST_CONTACT), ("The CEO", "2348034444444", OBSERVER)]


# ================================
# EXCEL
# ================================

def test_group_issues_excel_upsert(world, monkeypatch):
    from group_agent import excel as excel_module
    from group_agent.excel import COLUMNS, GroupIssuesExcel

    agent, store, bridge, llm = world
    report(agent, llm)
    calls = []
    table_rows = {"refs": []}

    def handler(request):
        url = str(request.url)
        calls.append((request.method, url, json.loads(request.content) if request.content else None))
        if request.method == "GET" and url.endswith("/tables"):
            return httpx.Response(200, json={"value": []})  # table doesn't exist yet
        if url.endswith("/tables/add"):
            table_rows["refs"] = [""]  # new table with one empty row
            return httpx.Response(201, json={"id": "{T1}"})
        if request.method == "GET" and url.endswith("/columns"):
            return httpx.Response(200, json={"value": [{"name": "Ref", "values": [["Ref"]] + [[r] for r in table_rows["refs"]]}]})
        return httpx.Response(200, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(excel_module.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    class Connection:
        async def _get_token(self, client):
            return "t"

        async def _get_workbook_url(self, client, headers):
            return "https://graph/workbook"

    excel = GroupIssuesExcel(Connection())
    assert asyncio.run(excel.sync(store)) == 1
    header_write = next(c for c in calls if "range(address='A1:" in c[1])
    assert header_write[2]["values"] == [COLUMNS]
    first_write = calls[-1]
    assert "rows/itemAt(index=0)/range" in first_write[1]  # filled the empty starter row
    row = first_write[2]["values"][0]
    assert row[0] == "MMF-G00001" and row[2] == NAME and row[4] == "'+2348012345678" and row[7] == "Open"
    assert asyncio.run(excel.sync(store)) == 0  # nothing changed

    table_rows["refs"] = ["MMF-G00099", "MMF-G00001"]  # someone sorted the table
    store.update_issue("MMF-G00001", status="In Progress", staff_response_at=int(time.time()), staff_name="Tunde Bakare")
    assert asyncio.run(excel.sync(store)) == 1
    assert "rows/itemAt(index=1)/range" in calls[-1][1]  # found by Ref, not position
    assert calls[-1][2]["values"][0][7] == "In Progress"


# ================================
# RESIDENT TAGS A STAFF MEMBER
# ================================

def tagged_report(agent, llm, area="Amenities & Bookings"):
    llm.then(("log_issue", {"message_id": "T1", "summary": "Pool for a Christmas function", "service_area": area}))        .then(("send_reply", {"text": "Logged as MMF-G00001.", "reply_to": "T1"}))
    msg(agent, "T1", "@Tunde can my company use the pool area in December?", mentions=["99887766@lid"])


def test_tagged_staff_get_ten_minutes_before_the_agent_acknowledges(world):
    agent, store, bridge, llm = world
    tagged_report(agent, llm)
    issue = store.get_issue("MMF-G00001")
    assert bridge.sent == [] and not issue["acknowledged_at"]        # held: the LLM's reply was not sent
    assert issue["ack_due_at"] == issue["opened_at"] + 600
    assert "(tagged staff)" in llm.prompts[-1]

    asyncio.run(agent.run_timers(now=issue["opened_at"] + 300))      # 5 minutes: still waiting
    assert bridge.sent == []
    asyncio.run(agent.run_timers(now=issue["opened_at"] + 601))      # 10 minutes, no staff answer
    assert len(bridge.sent) == 1 and bridge.sent[0]["quoted"] == "T1" and "MMF-G00001" in bridge.sent[0]["text"]
    assert store.get_issue("MMF-G00001")["acknowledged_at"]


def test_tagged_staff_answering_in_time_means_no_acknowledgement(world):
    agent, store, bridge, llm = world
    tagged_report(agent, llm)
    opened = store.get_issue("MMF-G00001")["opened_at"]
    msg(agent, "S1", "Good morning, do you mean the residents?", sender=TUNDE, name="Tunde", ts=opened + 240)
    asyncio.run(agent.run_timers(now=opened + 601))
    issue = store.get_issue("MMF-G00001")
    assert bridge.sent == [] and issue["status"] == "In Progress" and issue["staff_name"] == "Tunde Bakare"
    assert issue["staff_response_at"] == opened + 240 and not issue["ack_due_at"]


def test_resident_tagging_the_assistant_gets_an_answer(world):
    agent, store, bridge, llm = world
    llm.then(("send_reply", {"text": "Hello! I'm Max Migold's AI assistant.", "reply_to": "Q1"}))
    msg(agent, "Q1", "@Assistant are you a bot?", mentions=["me"])
    assert len(bridge.sent) == 1 and "(tagged you)" in llm.prompts[-1]


# ================================
# FOLLOW-UPS ON ISSUES STAFF TOOK ON
# ================================

def test_in_progress_issue_is_followed_up_privately(world):
    agent, store, bridge, llm = world
    report(agent, llm)
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "in_progress", "note": "will get back"}))
    msg(agent, "S1", "I will get back to you on this", sender=TUNDE, name="Tunde")
    started = store.get_issue("MMF-G00001")["staff_response_at"]
    before = len(bridge.sent)

    asyncio.run(agent.run_timers(now=started + 47 * 3600))
    assert len(bridge.sent) == before
    asyncio.run(agent.run_timers(now=started + 48 * 3600))           # first follow-up after 48 hours
    nudge = bridge.sent[-1]
    assert nudge["chat"] == TUNDE and "MMF-G00001" in nudge["text"] and "isn't marked as done" in nudge["text"]
    asyncio.run(agent.run_timers(now=started + 60 * 3600))           # not again until a day later
    assert len(bridge.sent) == before + 1
    for hours in (72, 96, 120, 144):                                  # then daily, three in total
        asyncio.run(agent.run_timers(now=started + hours * 3600))
    assert len(bridge.sent) == before + 3 and store.get_issue("MMF-G00001")["followups"] == 3


# ================================
# NO RATING FOR BILLING, BOOKINGS AND ENQUIRIES
# ================================

def test_billing_issue_closes_without_a_rating(world):
    agent, store, bridge, llm = world
    llm.then(("log_issue", {"message_id": "B1", "summary": "Asks how the electricity rate is calculated",
                            "service_area": "Billing/Energy"}))        .then(("send_reply", {"text": "Logged as MMF-G00001; the team will respond.", "reply_to": "B1"}))
    msg(agent, "B1", "Can you explain the calculation behind the rate?")
    before = len(bridge.sent)
    llm.then(("update_issue", {"ref": "MMF-G00001", "status": "resolved", "note": "details shared"}))
    msg(agent, "S1", "Here are the details of collections and expenses", sender=TUNDE, name="Tunde")
    issue = store.get_issue("MMF-G00001")
    assert issue["status"] == "Closed" and issue["score"] is None and issue["closed_at"]
    assert len(bridge.sent) == before                                 # no survey


def test_new_columns_added_to_an_old_database(tmp_path):
    import sqlite3
    from group_agent.store import GroupStore
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE group_issues (id INTEGER PRIMARY KEY, ref TEXT, chat_id TEXT, status TEXT, "
                "summary TEXT, opened_at INTEGER, synced INTEGER)")
    con.commit()
    con.close()
    store = GroupStore(path)
    columns = {r["name"] for r in store.db.query("PRAGMA table_info(group_issues)")}
    assert {"ack_due_at", "followups", "followed_up_at"} <= columns
    store.close()
