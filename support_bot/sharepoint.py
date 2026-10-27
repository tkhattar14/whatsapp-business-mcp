"""
Append rows to Excel tables in SharePoint via Microsoft Graph.

Two tables in the same workbook:
  - Conversations: every message (columns = EXCEL_COLUMNS)
  - Tickets: one row per completed intake (columns = ticket_columns(fields))

Uses app-only (client credentials) auth. Each table must already exist in the
workbook with exactly these columns, in the same order.
"""

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .config import SupportSettings
from .store import SupportStore

logger = logging.getLogger("whatsapp-support.sharepoint")

GRAPH_URL = "https://graph.microsoft.com/v1.0"

EXCEL_COLUMNS = [
    "Date/Time", "Phone", "Customer Name", "Direction", "Sent By",
    "Type", "Message", "Conversation State", "WhatsApp Message ID",
]


def ticket_columns(fields: List[str]) -> List[str]:
    """Ticket table columns: fixed ones, one per question in flow.json, then Status."""
    return ["Ticket #", "Date/Time", "Phone", "Customer Name", *fields, "Status"]

# Cells starting with these are interpreted by Excel as formulas.
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _cell(value: Any) -> Any:
    """Make a value safe to write to Excel: no formula injection from customer text."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _local_time(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def message_to_row(message: Dict[str, Any], tz: ZoneInfo) -> List[Any]:
    return [
        _local_time(message["ts"], tz),
        _cell("+" + message["phone"]),
        _cell(message.get("name")),
        "Incoming" if message["direction"] == "in" else "Outgoing",
        message["sender"].capitalize(),
        _cell(message.get("msg_type")),
        _cell(message.get("body")),
        _cell(message.get("state")),
        _cell(message.get("wamid")),
    ]


def ticket_to_row(ticket: Dict[str, Any], fields: List[str], tz: ZoneInfo) -> List[Any]:
    answers = ticket["answers"]
    return [
        ticket["ref"],
        _local_time(ticket["created_at"], tz),
        _cell("+" + ticket["phone"]),
        _cell(ticket.get("name")),
        *[_cell(answers.get(field, "")) for field in fields],
        _cell(ticket.get("status")),
    ]


class SharePointExcelLogger:
    """Copies unsynced messages and tickets from the local store into SharePoint Excel tables."""

    def __init__(self, settings: SupportSettings, store: SupportStore, ticket_fields: List[str]):
        self.settings = settings
        self.store = store
        self.ticket_fields = ticket_fields
        self.tz = ZoneInfo(settings.log_timezone)
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._workbook_url: Optional[str] = None

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        response = await client.post(
            f"https://login.microsoftonline.com/{self.settings.sharepoint_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.sharepoint_client_id,
                "client_secret": self.settings.sharepoint_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"Microsoft sign-in failed ({response.status_code}): {_graph_error(response)}")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    async def _get_workbook_url(self, client: httpx.AsyncClient, headers: Dict[str, str]) -> str:
        """Resolve site and file IDs once, then reuse the workbook URL."""
        if self._workbook_url:
            return self._workbook_url

        if self.settings.sharepoint_file_url:
            # A sharing link ("Copy link" in SharePoint) can be resolved straight to the file
            encoded = base64.urlsafe_b64encode(self.settings.sharepoint_file_url.encode()).decode().rstrip("=")
            response = await client.get(f"{GRAPH_URL}/shares/u!{encoded}/driveItem", headers=headers)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Could not open the Excel file from SHAREPOINT_FILE_URL ({response.status_code}): "
                    f"{_graph_error(response)}"
                )
            item = response.json()
            drive_id = item["parentReference"]["driveId"]
            self._workbook_url = f"{GRAPH_URL}/drives/{drive_id}/items/{item['id']}/workbook"
            return self._workbook_url

        site_path = "/" + self.settings.sharepoint_site_path.strip("/")
        response = await client.get(
            f"{GRAPH_URL}/sites/{self.settings.sharepoint_hostname}:{quote(site_path)}", headers=headers
        )
        if response.status_code != 200:
            raise RuntimeError(f"Could not find SharePoint site ({response.status_code}): {_graph_error(response)}")
        site_id = response.json()["id"]

        file_path = quote(self.settings.sharepoint_file_path.strip("/"))
        response = await client.get(f"{GRAPH_URL}/sites/{site_id}/drive/root:/{file_path}", headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"Could not find Excel file ({response.status_code}): {_graph_error(response)}")
        item_id = response.json()["id"]

        self._workbook_url = f"{GRAPH_URL}/sites/{site_id}/drive/items/{item_id}/workbook"
        return self._workbook_url

    async def _append_rows(self, table: str, rows: List[List[Any]]) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {await self._get_token(client)}"}
            workbook_url = await self._get_workbook_url(client, headers)
            response = await client.post(
                f"{workbook_url}/tables/{quote(table)}/rows", headers=headers, json={"values": rows}
            )
            if response.status_code not in (200, 201):
                if response.status_code == 404:
                    self._workbook_url = None  # file moved; resolve again next time
                raise RuntimeError(
                    f"Writing to Excel table '{table}' failed ({response.status_code}): {_graph_error(response)}"
                )

    async def get_ticket_status(self, ref: str) -> Optional[str]:
        """Read a ticket's current Status from the Excel Tickets table (the agent edits it there).
        Returns None if the ticket is not in the table yet."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {await self._get_token(client)}"}
            workbook_url = await self._get_workbook_url(client, headers)
            table = quote(self.settings.sharepoint_tickets_table)
            response = await client.get(f"{workbook_url}/tables/{table}/columns", headers=headers)
            if response.status_code != 200:
                if response.status_code == 404:
                    self._workbook_url = None
                raise RuntimeError(f"Reading Excel table failed ({response.status_code}): {_graph_error(response)}")

        # Each column's "values" is [[header], [row1], [row2], ...]
        columns = {c["name"]: [v[0] for v in c["values"][1:]] for c in response.json().get("value", [])}
        refs, statuses = columns.get("Ticket #"), columns.get("Status")
        if refs is None or statuses is None:
            raise RuntimeError("Tickets table needs 'Ticket #' and 'Status' columns")
        for row_ref, status in zip(refs, statuses):
            if str(row_ref).strip().upper() == ref:
                return str(status).strip() or None
        return None

    async def sync_once(self) -> int:
        """Push one batch of unsynced messages. Returns the number of rows written."""
        messages = self.store.get_unsynced(limit=200)
        if not messages:
            return 0
        await self._append_rows(self.settings.sharepoint_table_name, [message_to_row(m, self.tz) for m in messages])
        self.store.mark_synced([m["id"] for m in messages])
        return len(messages)

    async def sync_tickets_once(self) -> int:
        """Push unsynced tickets. Returns the number of rows written."""
        tickets = self.store.list_tickets(limit=100, unsynced_only=True)
        if not tickets:
            return 0
        await self._append_rows(
            self.settings.sharepoint_tickets_table,
            [ticket_to_row(t, self.ticket_fields, self.tz) for t in tickets],
        )
        self.store.mark_tickets_synced([t["id"] for t in tickets])
        return len(tickets)

    async def run(self, stop: asyncio.Event) -> None:
        """Background loop: sync every few seconds, backing off while SharePoint is failing."""
        delay = self.settings.excel_sync_interval
        while not stop.is_set():
            ok = True
            written = 0
            try:
                written = await self.sync_once()
                if written:
                    logger.info("Logged %d message(s) to SharePoint Excel", written)
            except Exception as e:
                ok = False
                logger.warning("SharePoint message sync failed, will retry: %s", e)
            try:
                tickets = await self.sync_tickets_once()
                if tickets:
                    logger.info("Logged %d ticket(s) to SharePoint Excel", tickets)
            except Exception as e:
                ok = False
                logger.warning("SharePoint ticket sync failed, will retry: %s", e)

            if ok:
                delay = self.settings.excel_sync_interval
                if written == 200:
                    continue  # more rows are waiting
            else:
                delay = min(delay * 2, 300)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


def _graph_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
        if isinstance(error, dict):
            return error.get("message") or error.get("code") or "unknown error"
        return response.json().get("error_description") or str(error)
    except ValueError:
        return response.text[:200]
