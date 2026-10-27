"""
Keeps the "Group Issues" table in the Complaints Log workbook up to date: one row per
issue, rewritten each time the issue moves on (acknowledged, reminded, resolved, rated).

Rows are found by their Ref, so people may sort and filter the table freely.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from support_bot.sharepoint import _cell

logger = logging.getLogger("group-agent.excel")

SHEET = "Group Issues"
TABLE = "GroupIssues"  # Excel table names can't contain spaces
COLUMNS = [
    "Ref", "Opened", "Group", "Resident", "Resident Phone", "Service Area", "Issue", "Status",
    "Acknowledged", "First Staff Response", "Response Time (min)", "Staff", "Reminders", "Escalated",
    "Resolved", "Resolved By", "Score", "Feedback", "Closed",
]
DATE_COLUMNS = {"Opened", "Acknowledged", "First Staff Response", "Escalated", "Resolved", "Closed"}
# Sent with every row so dates show as dates and phone numbers stay text
ROW_FORMAT = ["yyyy-mm-dd hh:mm" if c in DATE_COLUMNS else "@" if c == "Resident Phone" else "General" for c in COLUMNS]


def _when(ts: Optional[int], tz: ZoneInfo) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def issue_to_row(issue: Dict[str, Any], tz: ZoneInfo) -> List[Any]:
    resident = issue.get("resident_id") or ""
    phone = "" if resident.endswith("@lid") else resident.split("@")[0].split(":")[0]
    response = ""
    if issue.get("staff_response_at"):
        response = round((issue["staff_response_at"] - issue["opened_at"]) / 60)
    return [
        issue["ref"], _when(issue["opened_at"], tz), _cell(issue.get("chat_name")), _cell(issue.get("resident_name")),
        _cell("+" + phone) if phone else "", _cell(issue.get("service_area")), _cell(issue.get("summary")),
        issue["status"], _when(issue.get("acknowledged_at"), tz), _when(issue.get("staff_response_at"), tz), response,
        _cell(issue.get("staff_name")), (issue.get("reminders") or 0) + (issue.get("followups") or 0), _when(issue.get("escalated_at"), tz),
        _when(issue.get("resolved_at"), tz), _cell(issue.get("resolved_by")),
        "" if issue.get("score") is None else issue["score"], _cell(issue.get("feedback")), _when(issue.get("closed_at"), tz),
    ]


def _column_letter(n: int) -> str:
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


class GroupIssuesExcel:
    def __init__(self, excel_logger, tz_name: str = "Africa/Lagos"):
        self.excel = excel_logger  # support_bot SharePointExcelLogger: token + workbook location
        self.tz = ZoneInfo(tz_name)
        self._ready = False

    async def _client(self):
        client = httpx.AsyncClient(timeout=60)
        headers = {"Authorization": f"Bearer {await self.excel._get_token(client)}"}
        workbook = await self.excel._get_workbook_url(client, headers)
        return client, headers, workbook

    async def _ensure_table(self, client, headers, workbook) -> None:
        if self._ready:
            return
        tables = (await client.get(f"{workbook}/tables", headers=headers)).json().get("value", [])
        if not any(t["name"] == TABLE for t in tables):
            await client.post(f"{workbook}/worksheets/add", headers=headers, json={"name": SHEET})
            ws = f"{workbook}/worksheets('{SHEET}')"
            last = _column_letter(len(COLUMNS))
            await client.patch(f"{ws}/range(address='A1:{last}1')", headers=headers, json={"values": [COLUMNS]})
            response = await client.post(f"{ws}/tables/add", headers=headers,
                                         json={"address": f"'{SHEET}'!A1:{last}2", "hasHeaders": True})
            if response.status_code >= 300:
                raise RuntimeError(f"Could not create the {TABLE} table: {response.text[:200]}")
            await client.patch(f"{workbook}/tables('{response.json()['id']}')", headers=headers,
                               json={"name": TABLE, "style": "TableStyleMedium2"})
            logger.info("Created the %s sheet in the Complaints Log", SHEET)
        self._ready = True

    async def sync(self, store) -> int:
        """Write every changed issue to Excel. Returns how many rows were written."""
        issues = store.unsynced_issues()
        if not issues:
            return 0
        client, headers, workbook = await self._client()
        try:
            await self._ensure_table(client, headers, workbook)
            table = f"{workbook}/tables('{TABLE}')"
            columns = (await client.get(f"{table}/columns", headers=headers)).json().get("value", [])
            refs = [str(v[0]).strip() for v in columns[0]["values"][1:]] if columns else []
            written = []
            for issue in issues:
                row = issue_to_row(issue, self.tz)
                existing = issue["ref"] if issue["ref"] in refs else ("" if "" in refs else None)
                if existing is not None:  # update its row, or fill the table's empty starter row
                    index = refs.index(existing)
                    refs[index] = issue["ref"]
                else:
                    index = len(refs)
                    response = await client.post(f"{table}/rows", headers=headers, json={"values": [row]})
                    if response.status_code >= 300:
                        raise RuntimeError(f"Adding {issue['ref']} failed ({response.status_code}): {response.text[:200]}")
                    refs.append(issue["ref"])
                # Format first, then write, so phone numbers are kept as text
                row_range = f"{table}/rows/itemAt(index={index})/range"
                await client.patch(row_range, headers=headers, json={"numberFormat": [ROW_FORMAT]})
                response = await client.patch(row_range, headers=headers, json={"values": [row]})
                if response.status_code >= 300:
                    raise RuntimeError(f"Writing {issue['ref']} failed ({response.status_code}): {response.text[:200]}")
                written.append(issue)
            store.mark_issues_synced(written)
            return len(written)
        finally:
            await client.aclose()
