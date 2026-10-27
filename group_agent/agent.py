"""
The group agent: stores every WhatsApp message and, using an LLM with tools, runs each
resident issue from report to feedback:

  resident reports -> agent acknowledges + logs it (log_issue)
  staff responds   -> update_issue(in_progress)
  staff says done  -> update_issue(resolved) -> agent asks the resident for a 0-10 rating
  resident rates   -> record_feedback -> closed, score kept for NPS

Billing questions, bookings and general enquiries close when staff say done, without a rating.
When a resident tags a staff member, that person gets a few minutes to answer before the
agent acknowledges.

Timers (run_timers, plain code, not the LLM) remind staff in the group when nobody has
responded, escalate privately, follow up privately on issues staff took on but haven't
finished, and close issues whose rating never came.

Messages in a chat are batched for a few seconds (debounce) so a burst produces one
decision, and the agent's own replies are spaced out per chat.
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from .config import MODE_LISTEN, MODE_REPLY, GroupAgentSettings
from .llm import LLMError, build_llm
from .staff import ESCALATE_TO, FIRST_CONTACT, StaffDirectory
from .store import (
    AWAITING_FEEDBACK, CLOSED, CLOSED_NO_FEEDBACK, IN_PROGRESS, OPEN, GroupStore,
)

logger = logging.getLogger("group-agent")

MAX_TOOL_ROUNDS = 4
LLM_RETRY_SECONDS = 60  # LLM down (main and fallback): look at the chat again after this
SERVICE_AREAS = [
    "Plumbing", "Cleaning", "Security", "HVAC", "Civil Work", "Waste Management",
    "Landscaping", "Electrical", "Others",
]
# Not service jobs: closed without asking for a rating, so NPS reflects the work done
NO_SURVEY_AREAS = ["Billing/Energy", "Amenities & Bookings", "General Enquiry"]
ISSUE_AREAS = SERVICE_AREAS + NO_SURVEY_AREAS

TOOLS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "send_reply",
        "description": "Send a WhatsApp message in this chat. Only call this when a reply is really needed.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "The message to send (1-3 short sentences)."},
            "reply_to": {"type": "string", "description": "Id of the NEW message you are replying to."},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "log_issue",
        "description": "Record a NEW problem, request or question a resident has raised, so the team follows it up. "
                       "Call once per issue, then acknowledge it with send_reply quoting the ticket ref "
                       "(unless the resident tagged a staff member - then the system acknowledges later if needed).",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "string", "description": "Id of the resident's NEW message that reports it."},
            "summary": {"type": "string", "description": "The issue in a few words, e.g. 'No water in Flat 12 since 6am'."},
            "service_area": {"type": "string", "enum": ISSUE_AREAS},
        }, "required": ["message_id", "summary", "service_area"]},
    }},
    {"type": "function", "function": {
        "name": "update_issue",
        "description": "A Max Migold staff member (marked [STAFF]) has responded to an open issue or promised "
                       "to get back (status in_progress), or says it is done/fixed/answered (status resolved). "
                       "Never for residents' words.",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "The issue ref, e.g. MMF-G00012."},
            "status": {"type": "string", "enum": ["in_progress", "resolved"]},
            "note": {"type": "string", "description": "What the staff member said, in a few words."},
        }, "required": ["ref", "status"]},
    }},
    {"type": "function", "function": {
        "name": "record_feedback",
        "description": "The resident has answered the 0-10 rating question for an issue awaiting feedback.",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "score": {"type": "integer", "minimum": 0, "maximum": 10},
            "comment": {"type": "string", "description": "Their reason or comment, if they gave one."},
        }, "required": ["ref", "score"]},
    }},
]


class BridgeClient:
    """Talks to wa_bridge (the Node process that holds the WhatsApp connection)."""

    def __init__(self, url: str, secret: Optional[str]):
        self.url = url
        self.headers = {"X-Bridge-Secret": secret or ""}

    async def _get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get(f"{self.url}{path}", headers=self.headers)

    async def health(self) -> Dict[str, Any]:
        return (await self._get("/health")).json()

    async def groups(self) -> List[Dict[str, Any]]:
        return (await self._get("/groups")).json()

    async def qr_png(self) -> Optional[bytes]:
        response = await self._get("/qr")
        return response.content if response.status_code == 200 else None

    async def pairing_code(self, phone: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.url}/pair", headers=self.headers, json={"phone": phone})
        return {"status_code": response.status_code, **response.json()}

    async def send(
        self, chat: str, text: str, quoted: Optional[str] = None, mentions: Optional[List[str]] = None,
    ) -> str:
        """mentions = phone numbers, written in the text as @<number>."""
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.url}/send", headers=self.headers, json={
                "chat": chat, "text": text, "quoted": quoted, "mentions": mentions or [],
            })
        if response.status_code != 200:
            raise RuntimeError(f"Bridge could not send ({response.status_code}): {response.text[:200]}")
        return response.json().get("id")


class GroupAgent:
    def __init__(
        self, settings: GroupAgentSettings, store: GroupStore, bridge: BridgeClient, llm=None,
        staff: Optional[StaffDirectory] = None,
    ):
        self.settings = settings
        self.store = store
        self.bridge = bridge
        self.llm = llm or build_llm(settings)
        self.staff = staff or StaffDirectory()
        with open(settings.prompt_file, encoding="utf-8") as f:
            self.system_prompt = f.read().replace("{assistant_name}", settings.assistant_name)
        self.tz = ZoneInfo("Africa/Lagos")
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._timers: Dict[str, asyncio.Task] = {}
        # Messages stored before this start are context only - never answered after a restart
        row = store.db.one("SELECT MAX(id) AS id FROM group_messages")
        self._start_id = (row["id"] if row else None) or 0
        self._seen: Dict[str, int] = {}

    # ================================
    # INCOMING
    # ================================

    async def handle_event(self, event: Dict[str, Any]) -> None:
        if not event.get("chat") or not event.get("id"):
            return
        if not self.store.add_message(event):
            return  # duplicate, e.g. the echo of our own reply
        if event.get("from_me") or self.settings.mode == MODE_LISTEN:
            return
        if self.settings.debounce_seconds <= 0:
            await self.process_chat(event["chat"])
        else:
            self._schedule(event["chat"], self.settings.debounce_seconds)

    def may_send(self, chat_id: str, chat_name: Optional[str]) -> bool:
        """Reply mode, and this chat is on the allow-list (elsewhere the agent only observes)."""
        if self.settings.mode != MODE_REPLY:
            return False
        allowed = self.settings.reply_chats
        if "*" in allowed:
            return True
        name = (chat_name or "").strip().lower()
        return any(c == chat_id or (name and c.lower() == name) for c in allowed)

    # Kept for callers of the first version
    def may_reply_in(self, event: Dict[str, Any]) -> bool:
        return self.may_send(event["chat"], event.get("chat_name"))

    def _schedule(self, chat: str, delay: float) -> None:
        """Debounce: (re)start the timer so a burst of messages is decided on once."""
        if delay <= 0:
            asyncio.ensure_future(self.process_chat(chat))
            return
        timer = self._timers.get(chat)
        if timer and not timer.done():
            timer.cancel()
        self._timers[chat] = asyncio.ensure_future(self._after(chat, delay))

    async def _after(self, chat: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self.process_chat(chat)

    def stop(self) -> None:
        for timer in self._timers.values():
            timer.cancel()

    # ================================
    # DECIDING (LLM)
    # ================================

    async def process_chat(self, chat: str) -> None:
        async with self._locks[chat]:
            try:
                await self._decide(chat)
            except Exception:
                logger.exception("Group agent failed while processing chat %s", chat)

    async def _decide(self, chat: str) -> None:
        history = self.store.recent(chat, self.settings.history_size)
        seen = self._seen.get(chat, self._start_id)
        new = [m for m in history if m["id"] > seen and not m["from_me"]]
        if not new:
            return
        chat_name = next((m["chat_name"] for m in reversed(history) if m["chat_name"]), None)
        can_send = self.may_send(chat, chat_name)

        last_reply = self.store.last_reply_at(chat)
        wait = (last_reply or 0) + self.settings.min_reply_gap_seconds - time.time()
        if can_send and wait > 0:
            self._schedule(chat, wait)  # too soon after our last reply - look again later
            return

        decision = {"chat": chat, "chat_name": chat_name, "can_send": can_send, "replied": False, "sent": False,
                    "new": {m["msg_id"]: m for m in new}, "created": [], "handled": set()}
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self._transcript(chat, history, seen, can_send)},
        ]
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                answer = await self.llm.complete(messages, TOOLS)
            except LLMError as e:
                logger.warning("LLM unavailable, will retry in %d s: %s", LLM_RETRY_SECONDS, e)
                self._schedule(chat, LLM_RETRY_SECONDS)
                return
            calls = answer.get("tool_calls") or []
            if not calls:
                break
            messages.append({"role": "assistant", "content": answer.get("content"), "tool_calls": calls})
            for call in calls:
                result = await self._run_tool(call, decision)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": json.dumps(result)})
            if decision["replied"] and not decision["created"]:
                break  # replied and nothing left to record

        now = int(time.time())
        for ref in decision["created"]:
            if decision["sent"] and not self.store.get_issue(ref).get("ack_due_at"):
                self.store.update_issue(ref, acknowledged_at=now)
        await self._deterministic_follow_ups(decision)

        self._seen[chat] = max(m["id"] for m in new)
        logger.info("Chat %s: %d new message(s), %s%s", chat_name or chat, len(new),
                    "replied" if decision["replied"] else "stayed silent",
                    f", logged {', '.join(decision['created'])}" if decision["created"] else "")

    async def _run_tool(self, call: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
        function = call.get("function") or {}
        name = function.get("name")
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            return {"error": "arguments were not valid JSON"}
        try:
            if name == "send_reply":
                return await self._tool_send_reply(args, d)
            if name == "log_issue":
                return self._tool_log_issue(args, d)
            if name == "update_issue":
                return await self._tool_update_issue(args, d)
            if name == "record_feedback":
                return self._tool_record_feedback(args, d)
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return {"error": f"{name} failed: {type(e).__name__}"}
        return {"error": f"unknown tool {name}"}

    async def _tool_send_reply(self, args: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
        text = (args.get("text") or "").strip()
        if not text:
            return {"error": "text is empty"}
        if d["replied"]:
            return {"error": "already replied in this chat just now; do not send another message"}
        if not d["can_send"]:
            d["replied"] = True  # observe mode: record what it would have done, send nothing
            logger.info("Observe only - would have replied in %s: %s", d["chat_name"] or d["chat"], text)
            return {"sent": False, "note": "observe mode - message not sent; carry on recording issues"}
        # Only quote a message the LLM was shown as new; ignore made-up or stale ids
        reply_to = args.get("reply_to") if args.get("reply_to") in d["new"] else None
        if self._held_for_staff(reply_to, d):
            d["replied"] = True
            return {"sent": False, "note": "the resident tagged a staff member - give them time to answer; "
                                           "the system acknowledges later if they don't. Carry on recording issues."}
        await self._send(d["chat"], d["chat_name"], text, reply_to)
        d["replied"] = d["sent"] = True
        return {"sent": True}

    def _held_for_staff(self, reply_to: Optional[str], d: Dict[str, Any]) -> bool:
        """Don't answer a resident who tagged a staff member; with no reply_to, only if every resident tagged staff."""
        if reply_to:
            return _tags_staff(d["new"][reply_to])
        residents = [m for m in d["new"].values() if not self.staff.find(m["sender_id"], m["sender_name"])]
        return bool(residents) and all(_tags_staff(m) for m in residents)

    def _tool_log_issue(self, args: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
        message = d["new"].get(args.get("message_id"))
        if not message:
            return {"error": "message_id must be the id of a NEW message"}
        if self.staff.find(message["sender_id"], message["sender_name"]):
            return {"error": "that message is from a staff member, not a resident - do not log it as an issue"}
        for issue in self.store.active_issues(d["chat"]):
            if issue["reported_msg_id"] == message["msg_id"]:
                return {"ref": issue["ref"], "note": "already logged"}
        area = args.get("service_area") if args.get("service_area") in ISSUE_AREAS else "Others"
        summary = (args.get("summary") or message["text"] or "").strip()[:200]
        issue = self.store.create_issue(
            d["chat"], d["chat_name"], message["sender_id"], message["sender_name"], message["msg_id"],
            summary, area, opened_at=message["ts"],
        )
        d["created"].append(issue["ref"])
        if _tags_staff(message):
            wait = int(self.settings.tag_wait_minutes * 60)
            self.store.update_issue(issue["ref"], ack_due_at=message["ts"] + wait)
            return {"ref": issue["ref"], "next": f"do not reply - the resident tagged a staff member; if nobody from "
                                                 f"staff answers within {_minutes(wait)} the system acknowledges it"}
        return {"ref": issue["ref"], "next": "acknowledge it with send_reply, mentioning the ref"}

    async def _tool_update_issue(self, args: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
        issue = self.store.get_issue(args.get("ref"))
        if not issue or issue["chat_id"] != d["chat"]:
            return {"error": "no such issue in this group"}
        if issue["status"] not in (OPEN, IN_PROGRESS):
            return {"error": f"issue is already {issue['status']}"}
        staff_messages = [m for m in d["new"].values() if self.staff.find(m["sender_id"], m["sender_name"])]
        if self.staff.members and not staff_messages:
            return {"error": "only a staff member's message can update an issue; none of the NEW messages is from staff"}
        staff = (self.staff.find(staff_messages[-1]["sender_id"], staff_messages[-1]["sender_name"])
                 if staff_messages else None)
        staff_name = staff.name if staff else (staff_messages[-1]["sender_name"] if staff_messages else None)
        now = int(time.time())
        note = (args.get("note") or "").strip()
        notes = f"{issue['notes'] or ''}\n{note}".strip() if note else issue["notes"]
        fields: Dict[str, Any] = {"notes": notes}
        if not issue["staff_response_at"]:
            fields.update(staff_response_at=now, staff_name=staff_name)
        d["handled"].add(issue["ref"])

        if args.get("status") == "in_progress":
            self.store.update_issue(issue["ref"], status=IN_PROGRESS, **fields)
            return {"ref": issue["ref"], "status": IN_PROGRESS}

        if issue["service_area"] in NO_SURVEY_AREAS:
            self.store.update_issue(issue["ref"], status=CLOSED, resolved_at=now, resolved_by=staff_name,
                                    closed_at=now, **fields)
            logger.info("Issue %s (%s) closed - no rating asked", issue["ref"], issue["service_area"])
            return {"ref": issue["ref"], "status": CLOSED,
                    "note": "closed - no rating is asked for this kind of request; no message needed"}

        # Resolved: ask the resident for a rating, right there in the group
        self.store.update_issue(issue["ref"], status=AWAITING_FEEDBACK, resolved_at=now, resolved_by=staff_name, **fields)
        sent = await self._send_survey(self.store.get_issue(issue["ref"]))
        return {"ref": issue["ref"], "status": AWAITING_FEEDBACK,
                "survey": "sent to the resident - do not send another message about it" if sent else "not sent (observe mode)"}

    def _tool_record_feedback(self, args: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
        issue = self.store.get_issue(args.get("ref"))
        if not issue or issue["chat_id"] != d["chat"]:
            return {"error": "no such issue in this group"}
        if issue["status"] != AWAITING_FEEDBACK:
            return {"error": "this issue is not waiting for a rating"}
        try:
            score = int(args.get("score"))
        except (TypeError, ValueError):
            return {"error": "score must be a whole number from 0 to 10"}
        if not 0 <= score <= 10:
            return {"error": "score must be from 0 to 10"}
        self._close_with_score(issue, score, (args.get("comment") or "").strip())
        d["handled"].add(issue["ref"])
        return {"ref": issue["ref"], "status": CLOSED, "next": "you may thank them briefly with send_reply"}

    def _close_with_score(self, issue: Dict[str, Any], score: int, comment: str) -> None:
        self.store.update_issue(issue["ref"], status=CLOSED, score=score, feedback=comment or None,
                                closed_at=int(time.time()))
        logger.info("Issue %s closed with score %d", issue["ref"], score)

    async def _deterministic_follow_ups(self, d: Dict[str, Any]) -> None:
        """Safety nets that don't depend on the LLM noticing."""
        now = int(time.time())
        for issue in self.store.active_issues(d["chat"]):
            if issue["ref"] in d["handled"]:
                continue
            for m in d["new"].values():
                staff = self.staff.find(m["sender_id"], m["sender_name"])
                # A staff member replying directly to the resident's report counts as a response
                if (staff and issue["status"] == OPEN and not issue["staff_response_at"]
                        and m["quoted_id"] and m["quoted_id"] == issue["reported_msg_id"]):
                    self.store.update_issue(issue["ref"], status=IN_PROGRESS, staff_response_at=now, staff_name=staff.name)
                # A bare 0-10 from the resident who was asked counts as their rating
                if (issue["status"] == AWAITING_FEEDBACK and not staff and m["sender_id"] == issue["resident_id"]):
                    score = _bare_score(m["text"])
                    if score is not None:
                        self._close_with_score(issue, score, "")
                        break

    # ================================
    # TIMERS (plain code)
    # ================================

    async def run_timers(self, now: Optional[float] = None) -> None:
        now = int(now or time.time())
        for issue in self.store.active_issues():
            try:
                await self._issue_timers(issue, now)
            except Exception:
                logger.exception("Timer handling failed for %s", issue["ref"])

    async def _issue_timers(self, issue: Dict[str, Any], now: int) -> None:
        s = self.settings
        can_send = self.may_send(issue["chat_id"], issue["chat_name"])
        waiting = now - issue["opened_at"]

        if issue["status"] == OPEN and not issue["staff_response_at"]:
            # Staff may have answered without the LLM linking it to the issue: count any staff message
            due = issue.get("ack_due_at")
            if (due and now >= due) or waiting >= s.reminder_minutes * 60:
                answer = self.store.staff_message_since(
                    issue["chat_id"], issue["opened_at"], lambda m: self.staff.find(m["sender_id"], m["sender_name"]))
                if answer:
                    staff = self.staff.find(answer["sender_id"], answer["sender_name"])
                    self.store.update_issue(issue["ref"], status=IN_PROGRESS, staff_response_at=answer["ts"],
                                            staff_name=staff.name if staff else answer["sender_name"], ack_due_at=None)
                    return
            if due and now >= due and not issue["acknowledged_at"]:
                if can_send:
                    await self._send_late_ack(issue)
                    self.store.update_issue(issue["ref"], acknowledged_at=now, ack_due_at=None)
                else:
                    self.store.update_issue(issue["ref"], ack_due_at=None)
                return
            if issue["reminders"] == 0 and waiting >= s.reminder_minutes * 60 and can_send:
                await self._send_reminder(issue, waiting)
                self.store.update_issue(issue["ref"], reminders=1, reminded_at=now)
            elif (issue["reminders"] >= 1 and not issue["escalated_at"]
                  and waiting >= s.escalate_minutes * 60 and s.mode == MODE_REPLY):
                await self._send_escalation(issue, waiting)
                self.store.update_issue(issue["ref"], escalated_at=now)

        if issue["status"] == IN_PROGRESS and can_send and (issue.get("followups") or 0) < s.followup_max:
            started = issue["staff_response_at"] or issue["opened_at"]
            due = (issue["followed_up_at"] + s.followup_repeat_hours * 3600 if issue.get("followed_up_at")
                   else started + s.followup_hours * 3600)
            if now >= due:
                await self._send_followup(issue, now - started)
                self.store.update_issue(issue["ref"], followups=(issue.get("followups") or 0) + 1, followed_up_at=now)

        if issue["status"] == AWAITING_FEEDBACK:
            asked = issue["survey_sent_at"] or issue["resolved_at"] or issue["opened_at"]
            if now - asked >= s.feedback_hours * 3600:
                self.store.update_issue(issue["ref"], status=CLOSED_NO_FEEDBACK, closed_at=now)
                logger.info("Issue %s closed without feedback", issue["ref"])

    async def _send_reminder(self, issue: Dict[str, Any], waiting: int) -> None:
        contacts = self.staff.by_escalation(FIRST_CONTACT)
        tags = " ".join(f"@{m.phone}" for m in contacts) or "Team"
        text = (f"{tags}, a quick reminder: {issue['resident_name'] or 'a resident'} reported "
                f"\"{issue['summary']}\" ({issue['ref']}) {_minutes(waiting)} ago and is still waiting for a response. "
                f"Could one of you take this up, please?")
        await self._send(issue["chat_id"], issue["chat_name"], text, issue["reported_msg_id"],
                         mentions=[m.phone for m in contacts])
        logger.info("Reminder sent for %s", issue["ref"])

    async def _send_escalation(self, issue: Dict[str, Any], waiting: int) -> None:
        opened = _local(issue["opened_at"], self.tz)
        text = (f"Escalation from {self.settings.assistant_name}: {issue['ref']} in \"{issue['chat_name'] or 'a group'}\" - "
                f"{issue['resident_name'] or 'a resident'} reported \"{issue['summary']}\" at {opened}, "
                f"and no staff member has responded after {_minutes(waiting)}, even after a reminder in the group.")
        for member in self.staff.by_escalation(ESCALATE_TO):
            await self._send(member.jid, None, text)
        logger.info("Escalated %s to %d person(s)", issue["ref"], len(self.staff.by_escalation(ESCALATE_TO)))

    async def _send_late_ack(self, issue: Dict[str, Any]) -> None:
        who = issue["resident_name"] or "Thank you"
        text = (f"{who}, I've logged this as {issue['ref']} so it doesn't get missed. "
                f"The team will get back to you on it.")
        await self._send(issue["chat_id"], issue["chat_name"], text, issue["reported_msg_id"])
        logger.info("Acknowledged %s after staff didn't answer the tag", issue["ref"])

    async def _send_followup(self, issue: Dict[str, Any], since: int) -> None:
        """Private nudge to whoever took the issue on (or the first-contact staff if unknown)."""
        member = self.staff.find(None, issue["staff_name"]) if issue["staff_name"] else None
        recipients = [member] if member else self.staff.by_escalation(FIRST_CONTACT)
        opened = _local(issue["opened_at"], self.tz)
        text = (f"Follow-up from {self.settings.assistant_name}: {issue['ref']} in \"{issue['chat_name'] or 'a group'}\" - "
                f"{issue['resident_name'] or 'a resident'} raised \"{issue['summary']}\" on {opened}. "
                f"It was taken up {_minutes(since)} ago but isn't marked as done yet. "
                f"Once it's sorted, please say so in the group so it can be closed.")
        for recipient in recipients:
            await self._send(recipient.jid, None, text)
        logger.info("Followed up %s with %d person(s)", issue["ref"], len(recipients))

    async def _send_survey(self, issue: Dict[str, Any]) -> bool:
        if not self.may_send(issue["chat_id"], issue["chat_name"]):
            return False
        digits = issue["resident_id"].split("@")[0].split(":")[0] if issue["resident_id"] else ""
        who = f"@{digits}" if digits else (issue["resident_name"] or "Hello")
        text = (f"{who}, the team says your issue ({issue['summary']}) has been resolved. "
                f"On a scale of 0 to 10, how likely are you to recommend Max Migold to a neighbour? "
                f"You're welcome to add a short comment too.")
        await self._send(issue["chat_id"], issue["chat_name"], text, issue["reported_msg_id"],
                         mentions=[digits] if digits else None)
        self.store.update_issue(issue["ref"], survey_sent_at=int(time.time()))
        return True

    async def _send(
        self, chat: str, chat_name: Optional[str], text: str, quoted: Optional[str] = None,
        mentions: Optional[List[str]] = None,
    ) -> str:
        msg_id = await self.bridge.send(chat, text, quoted, mentions)
        self.store.add_message({
            "id": msg_id, "chat": chat, "chat_name": chat_name, "is_group": chat.endswith("@g.us"),
            "sender": "me", "sender_name": self.settings.assistant_name, "from_me": True, "type": "text",
            "text": text, "quoted_id": quoted, "timestamp": int(time.time()),
        })
        return msg_id

    # ================================
    # WHAT THE LLM SEES
    # ================================

    def _transcript(self, chat: str, history: List[Dict[str, Any]], seen: int, can_send: bool) -> str:
        """The chat as the LLM sees it. Names only - no phone numbers are sent to the LLM."""
        name = next((m["chat_name"] for m in reversed(history) if m["chat_name"]), None) or "this chat"
        parts = [f"WhatsApp group: {name}", ""]

        if self.staff.members:
            parts.append("MAX MIGOLD STAFF (their messages are marked [STAFF]):")
            parts += [f"- {m.name} ({m.role or 'staff'})" for m in self.staff.members]
            parts.append("")

        issues = self.store.active_issues(chat)
        if issues:
            parts.append("OPEN ISSUES in this group (refer to them by ref):")
            parts += [self._issue_line(i) for i in issues]
            parts.append("")

        earlier = [self._line(m) for m in history if m["id"] <= seen]
        new = [self._line(m) for m in history if m["id"] > seen]
        if earlier:
            parts += ["EARLIER MESSAGES (context only, already handled):", *earlier, ""]
        parts += ["NEW MESSAGES since you last looked:", *new, ""]
        if not can_send:
            parts.append("(You are observing this group: keep recording issues with the tools; your messages will not be sent.)")
        parts.append("Decide what the NEW messages need: log a new issue, update an issue from a staff message, "
                     "record a rating, and/or reply. If nothing is needed, do not call any tool.")
        return "\n".join(parts)

    def _issue_line(self, issue: Dict[str, Any]) -> str:
        opened = _local(issue["opened_at"], self.tz)
        who = issue["resident_name"] or "a resident"
        if issue["status"] == OPEN:
            state = "Open - no staff response yet"
            if issue.get("ack_due_at"):
                state += " (resident tagged staff - waiting for them to answer)"
        elif issue["status"] == IN_PROGRESS:
            state = f"In progress - {issue['staff_name'] or 'staff'} responded"
        else:
            state = f"Awaiting feedback - {who} was asked for a 0-10 rating"
        return f"- [{issue['ref']}] {issue['service_area']}: \"{issue['summary']}\" reported by {who} at {opened} - {state}"

    def _line(self, m: Dict[str, Any]) -> str:
        when = _local(m["ts"], self.tz)
        if m["from_me"]:
            who = f"You ({self.settings.assistant_name})"
        else:
            who = m["sender_name"] or "Someone"
            staff = self.staff.find(m["sender_id"], m["sender_name"])
            if staff:
                who = f"{staff.name} [STAFF, {staff.role or 'staff'}]"
        text = m["text"] or f"[{m['msg_type']}]"
        if m["msg_type"] not in ("text", "conversation", "extendedText") and m["text"]:
            text = f"[{m['msg_type']}] {text}"
        reply = f" (replying to msg {m['quoted_id']})" if m["quoted_id"] else ""
        if not m["from_me"] and "me" in _mentions(m):
            reply += " (tagged you)"
        elif not m["from_me"] and _tags_staff(m):
            reply += " (tagged staff)"
        # Phone numbers in @mentions are replaced, so they never reach the LLM
        text = re.sub(r"@\d{6,}", "@someone", text)
        return f"[msg {m['msg_id']}] {when} {who}{reply}: {text}"


def _local(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%d %b %H:%M")


def _minutes(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 90:
        return f"{minutes} minutes"
    return f"{minutes // 60} hours {minutes % 60} minutes" if minutes % 60 else f"{minutes // 60} hours"


def _mentions(message: Dict[str, Any]) -> List[str]:
    try:
        return json.loads(message.get("mentions") or "[]")
    except ValueError:
        return []


def _tags_staff(message: Dict[str, Any]) -> bool:
    """A resident tagged someone other than the assistant (in a flat group, that's staff)."""
    mentions = _mentions(message)
    return bool(mentions) and "me" not in mentions


def _bare_score(text: Optional[str]) -> Optional[int]:
    """'8', '8/10', '9 out of 10', 'I give 7' -> the number; anything longer -> None (left to the LLM)."""
    t = (text or "").strip().lower()
    match = re.fullmatch(r"(?:i give (?:you )?|i'd say |rating:? )?(10|[0-9])(?:\s*(?:/|out of)\s*10)?[.!]?", t)
    return int(match.group(1)) if match else None
