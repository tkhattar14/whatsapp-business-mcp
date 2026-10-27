"""
HTTP routes: the Meta webhook plus read-only conversation endpoints.
"""

import csv
import hashlib
import hmac
import io
import json
import os
from typing import Any, List
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from .service import SupportService
from .sharepoint import EXCEL_COLUMNS, message_to_row, ticket_columns, ticket_to_row

router = APIRouter()


def get_service(request: Request) -> SupportService:
    service = getattr(request.app.state, "support", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Support bot not available - check server configuration")
    return service


def require_api_key_configured() -> None:
    # Conversation data is personal data: never serve it unauthenticated,
    # even in the "dev mode" where the API key middleware is disabled.
    if not os.getenv("MCP_API_KEY"):
        raise HTTPException(status_code=503, detail="Set MCP_API_KEY to enable conversation endpoints")


def verify_signature(app_secret: str, payload: bytes, signature_header: str) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256="):])


# ================================
# META WEBHOOK
# ================================

@router.get("/webhook", include_in_schema=False)
async def verify_webhook(request: Request, service: SupportService = Depends(get_service)):
    """Meta calls this once when you save the callback URL in the App Dashboard."""
    params = request.query_params
    token = service.settings.verify_token
    if (
        token
        and params.get("hub.mode") == "subscribe"
        and hmac.compare_digest(params.get("hub.verify_token", "").encode(), token.encode())
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Webhook verification failed")


@router.post("/webhook", include_in_schema=False)
async def receive_webhook(
    request: Request, background_tasks: BackgroundTasks, service: SupportService = Depends(get_service)
):
    """Incoming messages, delivery statuses and agent echoes from Meta."""
    if not service.settings.app_secret:
        raise HTTPException(status_code=503, detail="META_APP_SECRET not configured")

    raw_body = await request.body()
    if not verify_signature(service.settings.app_secret, raw_body, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Acknowledge immediately; Meta retries if we are slow.
    background_tasks.add_task(service.bot.handle_webhook, payload)
    return {"status": "received"}


# ================================
# CONVERSATION DATA (protected by X-API-Key)
# ================================

@router.get("/api/v1/support/conversations", dependencies=[Depends(require_api_key_configured)])
async def list_conversations(limit: int = 100, service: SupportService = Depends(get_service)):
    return {"status": "success", "data": service.store.list_conversations(min(limit, 1000))}


@router.get("/api/v1/support/conversations/{phone}/messages", dependencies=[Depends(require_api_key_configured)])
async def conversation_messages(phone: str, service: SupportService = Depends(get_service)):
    return {"status": "success", "data": service.store.get_messages(phone=phone.lstrip("+"))}


@router.get("/api/v1/support/tickets", dependencies=[Depends(require_api_key_configured)])
async def list_tickets(limit: int = 100, service: SupportService = Depends(get_service)):
    return {"status": "success", "data": service.store.list_tickets(min(limit, 1000))}


@router.get("/api/v1/support/export.csv", dependencies=[Depends(require_api_key_configured)])
async def export_csv(service: SupportService = Depends(get_service)):
    """All logged messages as CSV (opens in Excel) - a fallback if SharePoint is unavailable."""
    tz = ZoneInfo(service.settings.log_timezone)
    rows = [message_to_row(m, tz) for m in service.store.get_messages(limit=1_000_000)]
    return _csv_response(EXCEL_COLUMNS, rows, "whatsapp-conversations.csv")


@router.get("/api/v1/support/tickets.csv", dependencies=[Depends(require_api_key_configured)])
async def export_tickets_csv(service: SupportService = Depends(get_service)):
    """All tickets as CSV, one row per completed intake."""
    tz = ZoneInfo(service.settings.log_timezone)
    fields = service.bot.fields
    tickets = reversed(service.store.list_tickets(limit=1_000_000))
    rows = [ticket_to_row(t, fields, tz) for t in tickets]
    return _csv_response(ticket_columns(fields), rows, "whatsapp-tickets.csv")


def _csv_response(header: List[str], rows: List[List[Any]], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content="﻿" + buffer.getvalue(),  # BOM so Excel detects UTF-8 (emoji, accents)
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
