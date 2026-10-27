"""
Relay mode: the human agent handles residents from their own WhatsApp.

- New tickets and residents' follow-up messages are forwarded to AGENT_WHATSAPP_NUMBER.
- The agent swipe-replies to a forwarded message (or starts with the ticket ID, e.g.
  "MMF-00012 we'll be there at 2pm"), and the reply is sent to the resident from the
  business number. Photos and documents work both ways.
- WhatsApp only lets the business message the agent within 24 hours of the agent's
  last message to it. Forwards that fail for that reason are queued and delivered as
  soon as the agent sends anything; optionally an approved template alerts the agent.
"""

import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from mcp_server import MessagingHandler

from .config import SupportSettings
from .store import STATE_AGENT, SupportStore, format_ticket_ref

logger = logging.getLogger("whatsapp-support.relay")

MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")
WINDOW_CLOSED = 131047  # Meta: more than 24 hours since the recipient last messaged us

AGENT_HELP = (
    "ℹ️ To reply to a resident, *swipe-reply* to one of their forwarded messages, "
    "or start your message with the ticket ID, e.g.\n*{example} We'll be there at 2pm.*"
)


def _meta_error(result: Dict[str, Any]) -> Tuple[Optional[int], str]:
    error = (result.get("error") or {}).get("error") or {}
    return error.get("code"), error.get("message") or str(result.get("message") or "unknown error")


def _message_id(result: Dict[str, Any]) -> Optional[str]:
    if result.get("status") != "success":
        return None
    return ((result.get("data") or {}).get("messages") or [{}])[0].get("id")


class AgentRelay:
    def __init__(self, settings: SupportSettings, store: SupportStore, messaging: MessagingHandler, ticket_prefix: str):
        self.settings = settings
        self.store = store
        self.messaging = messaging
        self.agent = settings.agent_number
        self.ticket_prefix = ticket_prefix
        self._ticket_at_start = re.compile(
            rf"\s*{ticket_prefix}\s*-?\s*0*(\d{{1,9}})\b[\s:,.-]*(.*)", re.IGNORECASE | re.DOTALL
        )

    def is_agent(self, phone: str) -> bool:
        return phone == self.agent

    # ================================
    # RESIDENT -> AGENT
    # ================================

    async def forward_ticket(
        self, phone: str, name: Optional[str], ticket: Dict[str, Any], last_message: Dict[str, Any]
    ) -> None:
        kind = ticket["answers"].get("Request Type", "ticket").lower()
        lines = [f"🆕 *New {kind} {ticket['ref']}*", f"👤 {name or 'Resident'} · +{phone}", ""]
        lines += [f"• {field}: {value}" for field, value in ticket["answers"].items()]
        lines += ["", "↩️ Swipe-reply to this message to answer the resident."]
        await self._deliver(
            {"kind": "text", "phone": phone, "ref": ticket["ref"], "text": "\n".join(lines)},
            alert=(kind, _place(ticket["answers"])),
        )

        # If the complaint itself was a photo or document, forward the file too
        if last_message.get("type") in MEDIA_TYPES:
            await self.forward_resident_message(phone, name, last_message, "")

    async def forward_resident_message(
        self, phone: str, name: Optional[str], message: Dict[str, Any], body: str
    ) -> None:
        ticket = self.store.latest_ticket(phone)
        ref = ticket["ref"] if ticket else None
        alert = ("message", _place(ticket["answers"]) if ticket else "a resident")
        header = f"💬 *{ref}* · {name or 'Resident'}" if ref else f"💬 {name or 'Resident'} · +{phone}"

        msg_type = message.get("type")
        if msg_type in MEDIA_TYPES:
            media = message.get(msg_type) or {}
            caption = header + (f":\n{media['caption']}" if media.get("caption") else "")
            payload = {
                "kind": "media", "phone": phone, "ref": ref, "media_type": msg_type,
                "media_id": media.get("id"), "caption": caption, "filename": media.get("filename"),
            }
        else:
            payload = {"kind": "text", "phone": phone, "ref": ref, "text": f"{header}:\n{body}"}
        await self._deliver(payload, alert=alert)

    async def _deliver(self, payload: Dict[str, Any], alert: Optional[Tuple[str, str]] = None) -> bool:
        """Send a forward to the agent. alert = (what, where) for the template, e.g. ("complaint", "La Tour")."""
        if alert:
            payload = {**payload, "alert": list(alert)}
        result = await self._send(self.agent, payload)
        wamid = _message_id(result)
        if wamid:
            self.store.add_relay_link(wamid, payload["phone"], payload.get("ref"), payload)
            return True

        code, reason = _meta_error(result)
        if code == WINDOW_CLOSED:
            await self._queue(payload)
        else:
            logger.warning("Forward to agent failed (Meta error %s): %s", code, reason)
        return False

    async def _queue(self, payload: Dict[str, Any]) -> None:
        waiting = self.store.enqueue_relay(payload)
        logger.info("Agent's 24h window is closed - queued forward (%d waiting)", waiting)
        # Alert for every new ticket, but only once for a run of follow-up messages
        alert = payload.get("alert")
        if alert and (alert[0] != "message" or waiting == 1):
            await self._alert_agent(*alert)

    async def handle_failed_status(self, wamid: str, code: Optional[int]) -> None:
        """Meta often accepts a forward and only reports later (status webhook) that the agent's
        24h window was closed. Queue it then, so it is delivered when the agent next writes."""
        if code != WINDOW_CLOSED:
            return
        link = self.store.get_relay_link(wamid)
        if link and link.get("payload"):
            await self._queue(json.loads(link["payload"]))

    async def _alert_agent(self, what: str, where: str) -> None:
        """Approved template messages can reach the agent even outside the 24h window."""
        if not self.settings.agent_alert_template:
            logger.warning("Set AGENT_ALERT_TEMPLATE so the agent is alerted when forwards are waiting")
            return
        result = await self.messaging._make_request("POST", self.messaging.messages_url, {
            "messaging_product": "whatsapp",
            "to": self.agent,
            "type": "template",
            "template": {
                "name": self.settings.agent_alert_template,
                "language": {"code": self.settings.agent_alert_language},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": what}, {"type": "text", "text": where},
                ]}],
            },
        })
        if result.get("status") != "success":
            logger.warning("Agent alert template failed: Meta error %s: %s", *_meta_error(result))

    async def flush_queue(self) -> None:
        for payload in self.store.take_relay_queue():
            payload.pop("alert", None)  # the agent is here now - no need to alert again
            await self._deliver(payload)

    # ================================
    # AGENT -> RESIDENT
    # ================================

    async def handle_agent_message(self, message: Dict[str, Any], body: str) -> None:
        if not self.store.mark_seen(message.get("id")):
            return  # webhook retry

        # The agent just messaged us, so their 24h window is open: deliver anything waiting
        await self.flush_queue()

        target = None
        context_id = (message.get("context") or {}).get("id")
        if context_id:
            target = self.store.get_relay_link(context_id)

        if not target and message.get("type") == "text":
            match = self._ticket_at_start.fullmatch(body)
            if match:
                ticket = self.store.get_ticket(format_ticket_ref(self.ticket_prefix, int(match.group(1))))
                if ticket:
                    target = {"phone": ticket["phone"], "ref": ticket["ref"]}
                    body = match.group(2).strip()

        msg_type = message.get("type")
        if not target:
            # A short "hi"/"ok" just reopens the agent's 24h window - no need to explain anything
            if msg_type == "text" and not context_id and not self._looks_like_reply(body):
                return
            await self._tell_agent(AGENT_HELP.format(example=format_ticket_ref(self.ticket_prefix, 12)))
            return
        if msg_type not in ("text", *MEDIA_TYPES) or (msg_type == "text" and not body.strip()):
            await self._tell_agent(AGENT_HELP.format(example=format_ticket_ref(self.ticket_prefix, 12)))
            return

        phone = target["phone"]
        if msg_type == "text":
            payload = {"kind": "text", "phone": phone, "ref": target.get("ref"), "text": body}
            logged = body
        else:
            media = message.get(msg_type) or {}
            payload = {
                "kind": "media", "phone": phone, "ref": target.get("ref"), "media_type": msg_type,
                "media_id": media.get("id"), "caption": media.get("caption"), "filename": media.get("filename"),
            }
            logged = " ".join(filter(None, [f"[{msg_type}]", media.get("filename"), media.get("caption")]))

        result = await self._send(phone, payload)
        wamid = _message_id(result)
        if not wamid:
            code, reason = _meta_error(result)
            if code == WINDOW_CLOSED:
                reason = "the resident hasn't messaged in the last 24 hours, so WhatsApp won't allow a reply until they write again"
            await self._tell_agent(f"❌ Not delivered to {target.get('ref') or '+' + phone}: {reason}")
            return

        conversation = self.store.get_conversation(phone) or {}
        self.store.add_message(
            phone, "out", "agent", msg_type, logged, wamid=wamid, name=conversation.get("name"), state=STATE_AGENT
        )
        self.store.upsert_conversation(phone, state=STATE_AGENT, last_agent_at=int(time.time()), step=None)
        await self.messaging.send_reaction_message(self.agent, message["id"], "✅")

    def _looks_like_reply(self, body: str) -> bool:
        return bool(re.search(rf"\b{self.ticket_prefix}\b", body, re.IGNORECASE)) or len(body.split()) > 3

    async def _tell_agent(self, text: str) -> None:
        await self.messaging.send_text_message(self.agent, text)

    # ================================
    # SENDING
    # ================================

    async def _send(self, to: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload["kind"] == "text":
            return await self.messaging.send_text_message(to, payload["text"])

        new_media_id = await self._rehost_media(payload["media_id"], payload.get("filename"))
        if not new_media_id:
            text = (payload.get("caption") or "") + f"\n[{payload['media_type']} could not be forwarded]"
            return await self.messaging.send_text_message(to, text.strip())
        return await self.messaging.send_media_message(
            to, payload["media_type"], media_id=new_media_id,
            caption=payload.get("caption"), filename=payload.get("filename"),
        )

    async def _rehost_media(self, media_id: Optional[str], filename: Optional[str]) -> Optional[str]:
        """Download a received photo/file from Meta and upload it again so it can be sent on."""
        if not media_id:
            return None
        try:
            headers = {"Authorization": f"Bearer {self.messaging.access_token}"}
            async with httpx.AsyncClient(timeout=60.0) as client:
                info = await client.get(f"{self.messaging.base_url}/{media_id}", headers=headers)
                info.raise_for_status()
                url, mime_type = info.json()["url"], info.json().get("mime_type", "application/octet-stream")
                download = await client.get(url, headers=headers)
                download.raise_for_status()

            mime_type = mime_type.split(";")[0].strip()
            result = await self.messaging._make_request("POST", self.messaging.media_url, files={
                "file": (filename or "media", download.content, mime_type),
                "messaging_product": (None, "whatsapp"),
                "type": (None, mime_type),
            })
            return (result.get("data") or {}).get("id")
        except Exception as e:
            logger.warning("Could not re-host media %s: %s", media_id, e)
            return None


def _place(answers: Dict[str, str]) -> str:
    """'La Tour (Flat 12)', 'Lagoon', ... for the agent alert."""
    location = answers.get("Location") or "a resident"
    flat = answers.get("Flat Number")
    return f"{location} (Flat {flat})" if flat else location
