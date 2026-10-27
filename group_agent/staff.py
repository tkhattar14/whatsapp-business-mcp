"""
The Max Migold team, read from the "Staff" table in the Complaints Log workbook on SharePoint.

Columns: Name | WhatsApp Number | Role | Escalation | Notes
Escalation: "First contact" (followed up in the group), "Escalate to" (messaged privately when
first contacts don't respond), "Observer" (never @mentioned; reports only).
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("group-agent.staff")

FIRST_CONTACT = "first contact"
ESCALATE_TO = "escalate to"
OBSERVER = "observer"


@dataclass(frozen=True)
class StaffMember:
    name: str
    phone: str  # digits with country code, e.g. 2348031234567
    role: str
    escalation: str  # one of the constants above
    notes: str = ""

    @property
    def jid(self) -> str:
        return f"{self.phone}@s.whatsapp.net"

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else self.phone


def normalise_phone(value: Any, default_country: str = "234") -> str:
    """'0803 123 4567' / '+234 803...' / 2348031234567.0 -> '2348031234567'."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d+\.0", text):  # Excel number formatting
        text = text[:-2]
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:  # Nigerian local format
        digits = default_country + digits[1:]
    return digits


def normalise_escalation(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("escal"):
        return ESCALATE_TO
    if text.startswith("observ") or text == "ceo":
        return OBSERVER
    return FIRST_CONTACT


def sender_digits(sender_id: Optional[str]) -> str:
    """'2348031234567:12@s.whatsapp.net' -> '2348031234567'; privacy ids (@lid) give ''."""
    if not sender_id or sender_id.endswith("@lid"):
        return ""
    return sender_id.split("@")[0].split(":")[0]


class StaffDirectory:
    def __init__(self, members: Optional[List[StaffMember]] = None):
        self.members: List[StaffMember] = members or []
        self.loaded_at: float = 0.0
        self.error: Optional[str] = None

    def find(self, sender_id: Optional[str], sender_name: Optional[str] = None) -> Optional[StaffMember]:
        digits = sender_digits(sender_id)
        if digits:
            for member in self.members:
                if member.phone == digits:
                    return member
            return None
        # A privacy id hides the number: fall back to the WhatsApp profile name
        name = (sender_name or "").strip().lower()
        if name:
            for member in self.members:
                if member.name.strip().lower() == name:
                    return member
        return None

    def by_escalation(self, level: str) -> List[StaffMember]:
        return [m for m in self.members if m.escalation == level]

    def summary(self) -> List[Dict[str, str]]:
        return [{"name": m.name, "role": m.role, "escalation": m.escalation} for m in self.members]


class SharePointStaffSource:
    """Loads the Staff table through the support bot's SharePoint connection."""

    def __init__(self, excel_logger, table: str = "Staff", refresh_seconds: float = 600):
        self.excel = excel_logger
        self.table = table
        self.refresh_seconds = refresh_seconds

    async def load(self) -> List[StaffMember]:
        async with httpx.AsyncClient(timeout=30) as client:
            headers = {"Authorization": f"Bearer {await self.excel._get_token(client)}"}
            workbook = await self.excel._get_workbook_url(client, headers)
            response = await client.get(f"{workbook}/tables('{self.table}')/rows", headers=headers)
            if response.status_code != 200:
                raise RuntimeError(f"Could not read the {self.table} table ({response.status_code}): {response.text[:200]}")
            columns_response = await client.get(f"{workbook}/tables('{self.table}')/columns", headers=headers)
        names = [c["name"].strip().lower() for c in columns_response.json().get("value", [])]
        members = []
        for row in response.json().get("value", []):
            values = dict(zip(names, row["values"][0]))
            name = str(values.get("name") or "").strip()
            phone = normalise_phone(values.get("whatsapp number"))
            if not name or len(phone) < 10:
                continue  # empty or incomplete row
            members.append(StaffMember(
                name=name, phone=phone, role=str(values.get("role") or "").strip(),
                escalation=normalise_escalation(values.get("escalation")), notes=str(values.get("notes") or "").strip(),
            ))
        return members

    async def refresh(self, directory: StaffDirectory) -> None:
        if time.time() - directory.loaded_at < self.refresh_seconds:
            return
        try:
            directory.members = await self.load()
            directory.error = None
            logger.info("Loaded %d staff member(s) from the %s sheet", len(directory.members), self.table)
        except Exception as e:
            directory.error = str(e)
            logger.warning("Could not refresh the staff list, keeping the previous one: %s", e)
        directory.loaded_at = time.time()
