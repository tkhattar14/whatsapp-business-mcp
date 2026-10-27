"""
HTTP routes for the group agent.

- POST /group-agent/events      events from wa_bridge (X-Bridge-Secret)
- GET  /api/v1/group-agent/...  status, login QR, groups, messages (X-API-Key)
"""

import hmac
import json
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response

from .service import GroupAgentService

router = APIRouter()


def get_service(request: Request) -> GroupAgentService:
    service = getattr(request.app.state, "group_agent", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Group agent is not enabled (set GROUP_AGENT_ENABLED=true)")
    return service


def require_api_key_configured() -> None:
    if not os.getenv("MCP_API_KEY"):
        raise HTTPException(status_code=503, detail="Set MCP_API_KEY to enable group agent endpoints")


@router.post("/group-agent/events", include_in_schema=False)
async def bridge_event(request: Request, background_tasks: BackgroundTasks,
                       service: GroupAgentService = Depends(get_service)):
    secret = service.settings.bridge_secret or ""
    given = request.headers.get("X-Bridge-Secret", "")
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="Invalid bridge secret")
    try:
        event = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    background_tasks.add_task(service.agent.handle_event, event)
    return {"status": "received"}


@router.get("/api/v1/group-agent/status", dependencies=[Depends(require_api_key_configured)])
async def status(service: GroupAgentService = Depends(get_service)):
    try:
        bridge = await service.bridge.health()
    except Exception as e:
        bridge = {"connected": False, "error": f"bridge unreachable: {type(e).__name__}"}
    s = service.settings
    return {
        "mode": s.mode,
        "reply_chats": list(s.reply_chats),
        "llm": service.agent.llm.name,
        "bridge": bridge,
        "staff": {"count": len(service.staff.members), "members": service.staff.summary(),
                  "error": service.staff.error},
        "timers": {"reminder_minutes": s.reminder_minutes, "escalate_minutes": s.escalate_minutes,
                   "feedback_hours": s.feedback_hours},
        "open_issues": len(service.store.active_issues()),
        "excel_error": service.excel_error,
        "chats": service.store.chats(),
    }


@router.post("/api/v1/group-agent/llm-check", dependencies=[Depends(require_api_key_configured)])
async def llm_check(service: GroupAgentService = Depends(get_service)):
    """Send one test conversation to the configured LLM (and to the fallback, if set), from this
    server, and report what came back."""
    from .llm import FallbackLLM

    llm = service.agent.llm
    if not service.settings.llm_configured:
        return {"ok": False, "llm": llm.name, "detail": "No LLM configured: set LLM_BASE_URL and LLM_MODEL"}
    if isinstance(llm, FallbackLLM):
        return {**await _check(service, llm.primary), "fallback": await _check(service, llm.fallback)}
    return await _check(service, llm)


async def _check(service: GroupAgentService, llm) -> dict:
    from .agent import TOOLS
    from .llm import LLMError

    messages = [
        {"role": "system", "content": service.agent.system_prompt},
        {"role": "user", "content": "WhatsApp group: Test group\n\nNEW MESSAGES since you last looked:\n"
                                    "[msg CHECK1] 23 Oct 09:00 Ada: Good morning, there's no water in my flat since 6am\n\n"
                                    "Decide whether any NEW message needs a reply. If not, do not call any tool."},
    ]
    try:
        answer = await llm.complete(messages, TOOLS)
    except LLMError as e:
        return {"ok": False, "llm": llm.name, "detail": str(e)}
    calls = answer.get("tool_calls") or []
    reply = None
    if calls:
        try:
            reply = json.loads(calls[0]["function"]["arguments"]).get("text")
        except (ValueError, KeyError, TypeError):
            reply = calls[0]["function"].get("arguments")
    return {
        "ok": True,
        "llm": llm.name,  # shows whether native tool calling or the JSON fallback is in use
        "would_reply": bool(calls),
        "reply": reply,
        "raw_content": (answer.get("content") or "")[:500] or None,
        "note": "Nothing was sent to WhatsApp - this was a dry run.",
    }


@router.get("/api/v1/group-agent/qr", dependencies=[Depends(require_api_key_configured)])
async def login_qr(service: GroupAgentService = Depends(get_service)):
    png = await service.bridge.qr_png()
    if not png:
        raise HTTPException(status_code=404, detail="No QR code: the number is already linked, or the bridge is starting")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/api/v1/group-agent/pair", dependencies=[Depends(require_api_key_configured)])
async def pairing_code(request: Request, service: GroupAgentService = Depends(get_service)):
    """Get an 8-character code to type in WhatsApp > Linked devices > Link with phone number."""
    body = await request.json()
    result = await service.bridge.pairing_code(str(body.get("phone", "")))
    status = result.pop("status_code")
    if status != 200:
        raise HTTPException(status_code=status, detail=result.get("error", "could not get a pairing code"))
    return result


@router.get("/api/v1/group-agent/groups", dependencies=[Depends(require_api_key_configured)])
async def groups(service: GroupAgentService = Depends(get_service)):
    return {"status": "success", "data": await service.bridge.groups()}


@router.get("/api/v1/group-agent/issues", dependencies=[Depends(require_api_key_configured)])
async def issues(limit: int = 200, service: GroupAgentService = Depends(get_service)):
    return {"status": "success", "data": service.store.list_issues(min(limit, 2000))}


@router.get("/api/v1/group-agent/messages", dependencies=[Depends(require_api_key_configured)])
async def messages(chat: str = None, limit: int = 200, service: GroupAgentService = Depends(get_service)):
    return {"status": "success", "data": service.store.list_messages(chat, min(limit, 2000))}
