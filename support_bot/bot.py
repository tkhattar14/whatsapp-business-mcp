"""
Intake bot with human handoff, designed for WhatsApp Coexistence.

The bot welcomes the customer and walks them through the questions in
flow.json (menu choices or free-text answers). When the flow reaches "done",
the answers are saved as a ticket, the customer is told their issue has been
recorded, and the conversation is handed to the human agent.

The agent replies from the WhatsApp Business app on the same number. Meta
reports those replies to the webhook as `smb_message_echoes`; when one arrives
the conversation switches to agent mode and the bot stays silent until the
agent has been inactive for AGENT_IDLE_HOURS.
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from mcp_server import MessagingHandler

from .config import SupportSettings
from .relay import AgentRelay
from .store import STATE_AGENT, STATE_BOT, SupportStore, format_ticket_ref

logger = logging.getLogger("whatsapp-support.bot")

DONE = "done"
MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")


# ================================
# FLOW DEFINITION
# ================================

def load_flow(path: str) -> Dict[str, Any]:
    """Load flow.json and check it is complete, so mistakes show at startup, not mid-chat."""
    with open(path, encoding="utf-8") as f:
        flow = json.load(f)

    for key in ("greeting", "start", "steps", "done"):
        if key not in flow:
            raise ValueError(f"{path}: missing '{key}'")
    steps = flow["steps"]
    if flow["start"] not in steps:
        raise ValueError(f"{path}: start step '{flow['start']}' does not exist")

    def check_target(step_id: str, target: Optional[str]) -> None:
        if target != DONE and target not in steps:
            raise ValueError(f"{path}: step '{step_id}' goes to unknown step '{target}'")

    for step_id, step in steps.items():
        if step.get("type") not in ("choice", "text"):
            raise ValueError(f"{path}: step '{step_id}' type must be 'choice' or 'text'")
        if not step.get("question") or not step.get("field"):
            raise ValueError(f"{path}: step '{step_id}' needs a 'question' and a 'field'")
        if step["type"] == "text":
            check_target(step_id, step.get("next"))
            if ("min" in step or "max" in step) and not all(isinstance(step.get(k), int) for k in ("min", "max")):
                raise ValueError(f"{path}: step '{step_id}' needs whole-number 'min' and 'max' together")
            continue

        options = step.get("options") or []
        if not 1 <= len(options) <= 10:
            raise ValueError(f"{path}: step '{step_id}' needs 1-10 options (WhatsApp limit)")
        limit = 20 if len(options) <= 3 else 24  # button / list row title limits
        for option in options:
            if not option.get("id") or not option.get("title"):
                raise ValueError(f"{path}: every option in '{step_id}' needs an 'id' and a 'title'")
            if len(option["title"]) > limit:
                raise ValueError(f"{path}: option '{option['title']}' is longer than {limit} characters")
            check_target(step_id, option.get("next") or step.get("next"))

    flow.setdefault("ticket_prefix", "TKT")
    flow.setdefault("ticket_status", "Pending")
    if not re.fullmatch(r"[A-Za-z]{1,10}", flow["ticket_prefix"]):
        raise ValueError(f"{path}: ticket_prefix must be 1-10 letters")
    flow["ticket_prefix"] = flow["ticket_prefix"].upper()
    statuses = flow.setdefault("ticket_statuses", [flow["ticket_status"]])
    if flow["ticket_status"] not in statuses:
        raise ValueError(f"{path}: ticket_status '{flow['ticket_status']}' must be one of ticket_statuses")
    for step_id, step in steps.items():
        # A final step may override the starting status; "" means no status (e.g. feedback)
        if step.get("ticket_status") and step["ticket_status"] not in statuses:
            raise ValueError(f"{path}: step '{step_id}' ticket_status must be one of ticket_statuses or \"\"")
    return flow


def canonical_status(value: str, statuses: List[str]) -> str:
    """Tidy a status typed in Excel: 'inprogress', 'in progress ', 'IN-PROGRESS' -> 'In Progress'."""
    squashed = re.sub(r"[\s_-]", "", value).lower()
    for status in statuses:
        if re.sub(r"[\s_-]", "", status).lower() == squashed:
            return status
    return value.strip()


def ticket_lookup_pattern(prefix: str) -> "re.Pattern[str]":
    """Matches a message that is just a ticket ID, e.g. 'MMF-00042', 'mmf 42', 'status MMF-42'."""
    return re.compile(
        rf"(?:(?:check\s+)?(?:ticket\s+)?status(?:\s+of)?|ticket|check)?\s*[:#]?\s*{prefix}\s*-?\s*0*(\d{{1,9}})",
        re.IGNORECASE,
    )


def flow_fields(flow: Dict[str, Any]) -> List[str]:
    """Answer fields in the order they appear in the flow (these become ticket columns)."""
    fields: List[str] = []
    for step in flow["steps"].values():
        if step["field"] not in fields:
            fields.append(step["field"])
    return fields


def describe_message(message: Dict[str, Any]) -> Tuple[str, str]:
    """Return (type, readable text) for any WhatsApp message object, for the log."""
    msg_type = message.get("type", "unknown")
    content = message.get(msg_type) or {}

    if msg_type == "text":
        return msg_type, content.get("body", "")
    if msg_type == "interactive":
        reply = content.get("button_reply") or content.get("list_reply")
        if reply:
            return msg_type, reply.get("title", "")
        return msg_type, f"[{content.get('type', 'interactive')} response]"
    if msg_type == "button":
        return msg_type, content.get("text", "")
    if msg_type in MEDIA_TYPES:
        parts = [f"[{msg_type}]"]
        if content.get("filename"):
            parts.append(content["filename"])
        if content.get("caption"):
            parts.append(content["caption"])
        if content.get("id"):
            parts.append(f"(media id: {content['id']})")
        return msg_type, " ".join(parts)
    if msg_type == "location":
        place = " ".join(filter(None, [content.get("name"), content.get("address")]))
        return msg_type, f"[location] {place} ({content.get('latitude')}, {content.get('longitude')})".replace("  ", " ")
    if msg_type == "contacts":
        names = [c.get("name", {}).get("formatted_name", "") for c in message.get("contacts", [])]
        return msg_type, "[contact] " + ", ".join(filter(None, names))
    if msg_type == "reaction":
        return msg_type, f"[reaction {content.get('emoji', '')}]".strip()
    return msg_type, f"[{msg_type}]"


class SupportBot:
    def __init__(self, settings: SupportSettings, store: SupportStore, messaging: MessagingHandler):
        self.settings = settings
        self.store = store
        self.messaging = messaging
        self.flow = load_flow(settings.flow_file)
        self.fields = flow_fields(self.flow)
        self._ticket_lookup = ticket_lookup_pattern(self.flow["ticket_prefix"])
        # Reads a ticket's live status from Excel; set by SupportService when SharePoint is configured
        self.status_reader: Optional[Callable[[str], Awaitable[Optional[str]]]] = None
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Relay mode: the agent replies from their own WhatsApp (otherwise Coexistence is assumed)
        self.relay = (
            AgentRelay(settings, store, messaging, self.flow["ticket_prefix"]) if settings.agent_number else None
        )

    # ================================
    # WEBHOOK ENTRY POINT
    # ================================

    async def handle_webhook(self, payload: Dict[str, Any]) -> None:
        if payload.get("object") != "whatsapp_business_account":
            return
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value") or {}

                # One Meta app can be subscribed to several numbers; only handle ours.
                metadata_id = (value.get("metadata") or {}).get("phone_number_id")
                if metadata_id and metadata_id != self.messaging.phone_number_id:
                    continue

                if field == "messages":
                    names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
                    for message in value.get("messages", []):
                        await self._safely(self.handle_customer_message(message, names.get(message.get("from"))))
                    for status in value.get("statuses", []):
                        await self._safely(self._handle_status(status))
                elif field == "smb_message_echoes":
                    for echo in value.get("message_echoes", []):
                        await self._safely(self.handle_agent_echo(echo))

    async def _safely(self, coro) -> None:
        try:
            await coro
        except Exception:
            logger.exception("Error while processing webhook event")

    async def _handle_status(self, status: Dict[str, Any]) -> None:
        if status.get("status") != "failed":
            return
        errors = status.get("errors") or [{}]
        logger.warning(
            "Message %s to %s failed: %s (code %s)",
            status.get("id"), status.get("recipient_id"), errors[0].get("title"), errors[0].get("code"),
        )
        if self.relay and self.relay.is_agent(status.get("recipient_id", "")):
            await self.relay.handle_failed_status(status.get("id"), errors[0].get("code"))

    # ================================
    # CUSTOMER MESSAGES
    # ================================

    async def handle_customer_message(self, message: Dict[str, Any], name: Optional[str]) -> None:
        phone = message["from"]
        ts = int(message.get("timestamp") or time.time())
        msg_type, body = describe_message(message)

        if self.relay and self.relay.is_agent(phone):
            await self.relay.handle_agent_message(message, body)
            return

        async with self._locks[phone]:
            conversation = self.store.get_conversation(phone)
            previous_customer_at = conversation["last_customer_at"] if conversation else None
            state = conversation["state"] if conversation else STATE_BOT

            is_new = self.store.add_message(
                phone, "in", "customer", msg_type, body, ts=ts, wamid=message.get("id"), name=name, state=state
            )
            if not is_new:
                return  # webhook retry of a message we already handled

            conversation = self.store.upsert_conversation(phone, name=name, last_customer_at=ts)

            # A message that is just a ticket ID is a status check, answered at any time
            lookup = self._ticket_lookup.fullmatch(_normalize(body))
            if lookup:
                ref = format_ticket_ref(self.flow["ticket_prefix"], int(lookup.group(1)))
                await self._reply_ticket_status(phone, conversation.get("name"), ref, conversation["state"])
                return

            if conversation["state"] == STATE_AGENT:
                if not self._agent_is_idle(conversation):
                    if self.relay:
                        await self.relay.forward_resident_message(phone, conversation.get("name"), message, body)
                    return  # the agent is handling this chat
                conversation = self.store.upsert_conversation(
                    phone, state=STATE_BOT, handoff_at=None, step=None, answers=None
                )

            await self._advance(phone, conversation, message, body, previous_customer_at)

    def _agent_is_idle(self, conversation: Dict[str, Any]) -> bool:
        last_activity = max(conversation.get("handoff_at") or 0, conversation.get("last_agent_at") or 0)
        return time.time() - last_activity > self.settings.agent_idle_hours * 3600

    async def _advance(
        self, phone: str, conversation: Dict[str, Any], message: Dict[str, Any], body: str,
        previous_customer_at: Optional[int],
    ) -> None:
        """Record the answer to the current question and move to the next one."""
        name = conversation.get("name")
        steps = self.flow["steps"]
        step_id = conversation.get("step")

        # Start (or restart) the intake: new chat, abandoned earlier, or asked to start over
        abandoned = (
            previous_customer_at is None
            or time.time() - previous_customer_at > self.settings.greeting_gap_hours * 3600
        )
        wants_restart = _normalize(body) in self.flow.get("restart_keywords", [])
        if step_id not in steps or abandoned or wants_restart:
            start = self.flow["start"]
            self.store.upsert_conversation(phone, step=start, answers="{}")
            await self._ask(phone, name, start, intro=self.flow["greeting"])
            return

        step = steps[step_id]
        answers = json.loads(conversation.get("answers") or "{}")

        if step["type"] == "choice":
            option = self._match_choice(step, message, body)
            if not option:
                await self._ask(phone, name, step_id, intro=self.flow.get("invalid_choice"))
                return
            answer = option.get("value") or option["title"]
            next_id = option.get("next") or step.get("next")
        else:
            answer = body.strip()
            if "min" in step:
                answer = _number_in_range(answer, step["min"], step["max"])
            if not answer:
                intro = step.get("invalid_answer") or self.flow.get("empty_answer")
                await self._ask(phone, name, step_id, intro=intro)
                return
            next_id = step["next"]

        field = step["field"]
        answers[field] = f"{answers[field]} / {answer}" if field in answers else answer

        if next_id == DONE:
            await self._finish(
                phone, name, answers, step.get("done") or self.flow["done"], message,
                step.get("ticket_status", self.flow["ticket_status"]),
            )
        else:
            self.store.upsert_conversation(phone, step=next_id, answers=json.dumps(answers, ensure_ascii=False))
            await self._ask(phone, name, next_id)

    async def _finish(
        self, phone: str, name: Optional[str], answers: Dict[str, str], done_text: str,
        last_message: Dict[str, Any], status: str,
    ) -> None:
        """Save the ticket, confirm to the customer, and hand over to the agent."""
        ticket = self.store.create_ticket(phone, name, answers, self.flow["ticket_prefix"], status or None)
        self.store.upsert_conversation(
            phone, state=STATE_AGENT, handoff_at=int(time.time()), step=None,
            answers=json.dumps(answers, ensure_ascii=False),
        )
        summary = "\n".join(f"• {field}: {value}" for field, value in answers.items())
        text = done_text.replace("{ticket}", ticket["ref"]).replace("{summary}", summary)
        await self._send_text(phone, name, text, STATE_AGENT)
        logger.info("Ticket %s recorded for %s - handed off to agent", ticket["ref"], phone)
        if self.relay:
            await self.relay.forward_ticket(phone, name, ticket, last_message)

    async def _reply_ticket_status(self, phone: str, name: Optional[str], ref: str, state: str) -> None:
        ticket = self.store.get_ticket(ref)
        if not ticket or ticket["phone"] != phone:
            # Ticket IDs are sequential, so only reveal tickets raised from this same number
            text = self.flow.get("status_not_found", "Sorry, we couldn't find ticket {ticket} for this number.")
            await self._send_text(phone, name, text.replace("{ticket}", ref), state)
            return

        status = ticket.get("status")
        if self.status_reader and ticket["synced"]:
            try:
                excel_status = await self.status_reader(ref)
                if excel_status:
                    status = canonical_status(excel_status, self.flow["ticket_statuses"])
                    self.store.set_ticket_status(ref, status)
            except Exception as e:
                logger.warning("Could not read status of %s from Excel, using last known: %s", ref, e)

        if status:
            text = self.flow.get("status_reply", "Ticket {ticket} - Status: *{status}*")
        else:  # e.g. feedback, which has no status unless the agent sets one
            text = self.flow.get("status_reply_no_status", "Thank you - {ticket} was received.")
        text = text.replace("{ticket}", ref).replace("{status}", status or "")
        text = text.replace("{summary}", "\n".join(f"• {f}: {v}" for f, v in ticket["answers"].items()))
        await self._send_text(phone, name, text, state)

    def _match_choice(self, step: Dict[str, Any], message: Dict[str, Any], body: str) -> Optional[Dict[str, Any]]:
        options: List[Dict[str, Any]] = step["options"]

        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        if reply:
            return next((o for o in options if o["id"] == reply.get("id")), None)

        # Typed answers: the option number, its title, or one of its keywords
        normalized = _normalize(body)
        if normalized.isdigit() and 1 <= int(normalized) <= len(options):
            return options[int(normalized) - 1]
        for option in options:
            if normalized == option["title"].lower() or normalized in option.get("keywords", []):
                return option
        return None

    # ================================
    # AGENT REPLIES (Coexistence echoes)
    # ================================

    async def handle_agent_echo(self, echo: Dict[str, Any]) -> None:
        phone = echo.get("to")
        if not phone:
            return
        wamid = echo.get("id")
        ts = int(echo.get("timestamp") or time.time())
        msg_type, body = describe_message(echo)

        async with self._locks[phone]:
            if wamid and self.store.has_message(wamid):
                return  # a bot message we already logged, or a webhook retry
            conversation = self.store.get_conversation(phone) or {}
            self.store.add_message(
                phone, "out", "agent", msg_type, body, ts=ts, wamid=wamid,
                name=conversation.get("name"), state=STATE_AGENT,
            )
            # The agent may step in at any point, even mid-questionnaire
            self.store.upsert_conversation(phone, state=STATE_AGENT, last_agent_at=ts, step=None)

    # ================================
    # SENDING (always logged)
    # ================================

    async def _ask(self, phone: str, name: Optional[str], step_id: str, intro: Optional[str] = None) -> None:
        step = self.flow["steps"][step_id]
        text = f"{intro}\n\n{step['question']}" if intro else step["question"]
        if step["type"] == "text":
            await self._send_text(phone, name, text, STATE_BOT)
            return

        text = _personalize(text, name)
        options = step["options"]
        if len(options) <= 3:
            result = await self.messaging.send_button_message(
                phone, text, [{"id": o["id"], "title": o["title"]} for o in options]
            )
        else:
            result = await self.messaging.send_list_message(
                phone, text, self.flow.get("list_button", "See options"),
                [{"title": "Options", "rows": [{"id": o["id"], "title": o["title"]} for o in options]}],
            )
        logged = f"{text}\n[Options: {' | '.join(o['title'] for o in options)}]"
        self._log_outgoing(phone, name, "interactive", logged, result, STATE_BOT)

    async def _send_text(self, phone: str, name: Optional[str], text: str, state: str) -> None:
        text = _personalize(text, name)
        result = await self.messaging.send_text_message(phone, text)
        self._log_outgoing(phone, name, "text", text, result, state)

    def _log_outgoing(
        self, phone: str, name: Optional[str], msg_type: str, body: str, result: Dict[str, Any], state: str
    ) -> None:
        wamid = None
        if result.get("status") == "success":
            messages = (result.get("data") or {}).get("messages") or [{}]
            wamid = messages[0].get("id")
        else:
            error = (result.get("error") or {}).get("error") or {}
            logger.warning(
                "Bot reply to %s failed: %s - Meta error %s: %s",
                phone, result.get("message"), error.get("code"), error.get("message") or result.get("error"),
            )
            body = "[NOT DELIVERED] " + body
        self.store.add_message(phone, "out", "bot", msg_type, body, wamid=wamid, name=name, state=state)


def _number_in_range(text: str, low: int, high: int) -> str:
    """Accept '12', 'Flat 12', 'no. 12' etc. Returns the number, or '' if not exactly one valid number."""
    numbers = re.findall(r"\d+", text)
    if len(numbers) == 1 and low <= int(numbers[0]) <= high:
        return str(int(numbers[0]))
    return ""


def _normalize(text: str) -> str:
    return " ".join(text.lower().strip().strip(".!?,").split())


def _personalize(text: str, name: Optional[str]) -> str:
    first_name = (name or "").split(" ")[0] or "there"
    return text.replace("{name}", first_name)
