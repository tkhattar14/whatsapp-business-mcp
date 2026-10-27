import os
import sys
import asyncio
import hmac
import json
import logging
import re
import time
from collections import defaultdict
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
from dotenv import load_dotenv

load_dotenv()

# Startup messages contain emoji; don't crash on Windows consoles that aren't UTF-8.
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

# Import our MCP server components with graceful error handling
try:
    from mcp_server import (
        MessagingHandler, TemplateHandler, BusinessHandler, MediaHandler,
        WhatsAppHandler
    )
    print("✅ Core handlers imported successfully")
except ImportError as e:
    print(f"⚠️ Warning: Could not import some handlers: {e}")
    MessagingHandler = TemplateHandler = BusinessHandler = MediaHandler = WhatsAppHandler = None

# Import extended handlers with graceful error handling
try:
    from mcp_server import (
        FlowHandler, AnalyticsHandler, WebhookHandler, BusinessAccountHandler
    )
    print("✅ Extended handlers imported successfully")
except ImportError as e:
    print(f"⚠️ Warning: Could not import extended handlers: {e}")
    FlowHandler = AnalyticsHandler = WebhookHandler = BusinessAccountHandler = None

# Import comprehensive tools with graceful error handling
try:
    from comprehensive_tools import register_comprehensive_tools
    print("✅ Comprehensive tools imported successfully")
except ImportError as e:
    print(f"⚠️ Warning: Could not import comprehensive tools: {e}")
    register_comprehensive_tools = None

try:
    from comprehensive_tools_extended import register_comprehensive_tools_extended
    print("✅ Extended comprehensive tools imported successfully")
except ImportError as e:
    print(f"⚠️ Warning: Could not import extended comprehensive tools: {e}")
    register_comprehensive_tools_extended = None

from support_bot import SupportService, SupportSettings, router as support_router
from group_agent import GroupAgentService, GroupAgentSettings, router as group_agent_router

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-mcp-http")


class RedactVerifyTokenFilter(logging.Filter):
    """Keep the webhook verify token (sent by Meta as a query parameter) out of the access log."""

    _pattern = re.compile(r"(hub[._]verify_token=)[^&\s]*")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._pattern.sub(r"\1[REDACTED]", arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(RedactVerifyTokenFilter())


# ================================
# RATE LIMITER (in-memory token bucket)
# ================================

class TokenBucketRateLimiter:
    """Simple in-memory token bucket rate limiter. Per-IP, 10 requests/second."""

    def __init__(self, rate: float = 10.0, capacity: float = 10.0):
        self.rate = rate          # tokens added per second
        self.capacity = capacity  # max burst size
        self._buckets: Dict[str, list] = defaultdict(lambda: [capacity, time.monotonic()])

    def allow(self, key: str) -> bool:
        tokens, last_time = self._buckets[key]
        now = time.monotonic()
        elapsed = now - last_time
        tokens = min(self.capacity, tokens + elapsed * self.rate)
        self._buckets[key] = [tokens, now]
        if tokens >= 1.0:
            self._buckets[key][0] = tokens - 1.0
            return True
        return False


rate_limiter = TokenBucketRateLimiter(rate=10.0, capacity=10.0)


# ================================
# SECURITY MIDDLEWARE
# ================================

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Set HSTS only when request came over HTTPS
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store"
        return response


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Require X-API-Key header on /api/* endpoints when MCP_API_KEY is configured."""

    async def dispatch(self, request: Request, call_next):
        api_key = os.getenv("MCP_API_KEY")
        # If MCP_API_KEY is not set, skip auth (dev mode)
        if api_key and request.url.path.startswith("/api/"):
            provided_key = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(provided_key.encode(), api_key.encode()):
                return JSONResponse(
                    status_code=401,
                    content={"status": "error", "message": "Invalid or missing API key"},
                )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limit requests per client IP."""

    async def dispatch(self, request: Request, call_next):
        # Only rate-limit API endpoints
        if request.url.path.startswith("/api/"):
            client_ip = (
                request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "unknown")
            )
            if not rate_limiter.allow(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"status": "error", "message": "Rate limit exceeded. Max 10 requests/second."},
                )
        return await call_next(request)


# ================================
# APP SETUP
# ================================

app = FastAPI(
    title="WhatsApp Cloud API MCP Server",
    description="Comprehensive WhatsApp Cloud API integration with 50+ tools including Flows, Analytics, Webhooks, and Business Account Management",
    version="2.0.0"
)

# CORS: only allow configured origins (empty = no CORS)
allowed_origins_str = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins = [o.strip() for o in allowed_origins_str.split(",") if o.strip()] if allowed_origins_str else []

if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Order matters: outermost middleware runs first.
# SecurityHeaders -> RateLimit -> APIKeyAuth -> handler
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(APIKeyAuthMiddleware)

# WhatsApp webhook (/webhook) and support conversation endpoints (/api/v1/support/*)
app.include_router(support_router)
# WhatsApp group agent (/group-agent/events from wa_bridge, /api/v1/group-agent/*)
app.include_router(group_agent_router)

# Global handlers
messaging_handler = None
template_handler = None
business_handler = None
media_handler = None
flow_handler = None
analytics_handler = None
webhook_handler = None
business_account_handler = None
whatsapp_handler = None

@app.on_event("startup")
async def startup_event():
    """Initialize handlers on startup"""
    global messaging_handler, template_handler, business_handler, media_handler, whatsapp_handler
    global flow_handler, analytics_handler, webhook_handler, business_account_handler

    logger.info("Starting WhatsApp MCP HTTP Server")
    logger.info(f"Server will be available at: http://0.0.0.0:{os.getenv('PORT', '8080')}")
    logger.info(f"API Documentation: http://0.0.0.0:{os.getenv('PORT', '8080')}/docs")
    logger.info(f"Health Check: http://0.0.0.0:{os.getenv('PORT', '8080')}/health")

    # Log security config
    if os.getenv("MCP_API_KEY"):
        logger.info("API key authentication ENABLED for /api/* endpoints")
    else:
        logger.warning("MCP_API_KEY not set - API key auth disabled (dev mode)")
    if allowed_origins:
        logger.info(f"CORS allowed origins: {allowed_origins}")
    else:
        logger.info("CORS disabled (no ALLOWED_ORIGINS configured)")

    # Check required environment variables
    required_vars = ['META_ACCESS_TOKEN', 'META_PHONE_NUMBER_ID']
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars:
        logger.warning(f"Missing required environment variables: {missing_vars}")
        logger.warning("Starting in demo mode - some features may be limited")
        logger.info("Set META_ACCESS_TOKEN and META_PHONE_NUMBER_ID for full functionality")
    else:
        logger.info("All required environment variables found")

    try:
        # Initialize core handlers
        if MessagingHandler:
            messaging_handler = MessagingHandler()
            logger.info("Messaging handler initialized")

        if TemplateHandler:
            template_handler = TemplateHandler()
            logger.info("Template handler initialized")

        if BusinessHandler:
            business_handler = BusinessHandler()
            logger.info("Business handler initialized")

        if MediaHandler:
            media_handler = MediaHandler()
            logger.info("Media handler initialized")

        if WhatsAppHandler:
            whatsapp_handler = WhatsAppHandler()
            logger.info("WhatsApp handler initialized")

        # Initialize extended handlers (optional)
        if FlowHandler:
            try:
                flow_handler = FlowHandler()
                logger.info("Flow handler initialized")
            except Exception as e:
                logger.warning(f"Flow handler not initialized: {e}")
                flow_handler = None

        if AnalyticsHandler:
            try:
                analytics_handler = AnalyticsHandler()
                logger.info("Analytics handler initialized")
            except Exception as e:
                logger.warning(f"Analytics handler not initialized: {e}")
                analytics_handler = None

        if WebhookHandler:
            try:
                webhook_handler = WebhookHandler()
                logger.info("Webhook handler initialized")
            except Exception as e:
                logger.warning(f"Webhook handler not initialized: {e}")
                webhook_handler = None

        if BusinessAccountHandler:
            try:
                business_account_handler = BusinessAccountHandler()
                logger.info("Business Account handler initialized")
            except Exception as e:
                logger.warning(f"Business Account handler not initialized: {e}")
                business_account_handler = None

        # Customer support bot (menu + agent handoff + Excel logging)
        if messaging_handler:
            try:
                app.state.support = SupportService(SupportSettings.from_env(), messaging_handler)
                await app.state.support.start()
                logger.info("Support bot initialized - webhook at /webhook")
            except Exception as e:
                logger.error(f"Support bot not initialized: {e}")
                app.state.support = None

        # WhatsApp group agent (dedicated number in residents' groups, via wa_bridge)
        app.state.group_agent = None
        group_settings = GroupAgentSettings.from_env()
        if group_settings.enabled:
            try:
                app.state.group_agent = GroupAgentService(group_settings)
                await app.state.group_agent.start()
            except Exception as e:
                logger.error(f"Group agent not initialized: {e}")
                app.state.group_agent = None

        logger.info("All available handlers initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing handlers: {e}")
        logger.info("Starting in demo mode")

@app.on_event("shutdown")
async def shutdown_event():
    support = getattr(app.state, "support", None)
    if support:
        await support.stop()
    group_agent = getattr(app.state, "group_agent", None)
    if group_agent:
        await group_agent.stop()

@app.get("/")
async def root():
    """Root endpoint with server status"""
    status = "demo_mode" if not os.getenv('META_ACCESS_TOKEN') else "active"

    return {
        "status": status,
        "message": "WhatsApp Cloud API MCP HTTP Server",
        "version": "2.0.0",
        "description": "Comprehensive WhatsApp Cloud API integration with 50+ tools",
        "endpoints": {
            "health": "/health",
            "docs": "/docs",
            "messaging": "/api/v1/messaging",
            "templates": "/api/v1/templates",
            "business": "/api/v1/business",
            "media": "/api/v1/media",
            "flows": "/api/v1/flows",
            "analytics": "/api/v1/analytics",
            "webhooks": "/api/v1/webhooks",
            "business_account": "/api/v1/business-account"
        },
        "features": [
            "Text, media, interactive messaging",
            "Template management and sending",
            "WhatsApp Flows (interactive experiences)",
            "Analytics and metrics",
            "Webhook management",
            "Business account management",
            "Advanced phone operations"
        ]
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "service": "WhatsApp Cloud API MCP Server",
        "handlers": {
            "messaging": messaging_handler is not None,
            "templates": template_handler is not None,
            "business": business_handler is not None,
            "media": media_handler is not None,
            "whatsapp": whatsapp_handler is not None,
            "flows": flow_handler is not None,
            "analytics": analytics_handler is not None,
            "webhooks": webhook_handler is not None,
            "business_account": business_account_handler is not None,
            "support_bot": getattr(app.state, "support", None) is not None,
            "support_storage": app.state.support.store.backend if getattr(app.state, "support", None) else None,
        },
        "features": {
            "total_tools": "50+ WhatsApp Cloud API tools",
            "messaging": "Text, media, interactive, location, contact",
            "templates": "Create, send, manage message templates",
            "flows": "Interactive WhatsApp experiences",
            "analytics": "Conversation, quality, business metrics",
            "webhooks": "Event subscription management",
            "business": "Account and phone number management"
        }
    }

# ================================
# MESSAGING ENDPOINTS
# ================================

@app.post("/api/v1/messaging/send")
async def send_message(request: Dict[str, Any]):
    """Send a WhatsApp message"""
    if not messaging_handler:
        raise HTTPException(status_code=503, detail="Messaging handler not available")

    try:
        phone_number = request.get("phone_number")
        message = request.get("message")
        preview_url = request.get("preview_url", False)
        reply_to_message_id = request.get("reply_to_message_id")

        if not phone_number or not message:
            raise HTTPException(status_code=400, detail="phone_number and message are required")

        result = await messaging_handler.send_text_message(phone_number, message, preview_url, reply_to_message_id)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return {"status": "error", "error": "An error occurred while sending the message"}

@app.post("/api/v1/messaging/reaction")
async def send_reaction(request: Dict[str, Any]):
    """Send a reaction to a message"""
    if not messaging_handler:
        raise HTTPException(status_code=503, detail="Messaging handler not available")

    try:
        phone_number = request.get("phone_number")
        message_id = request.get("message_id")
        emoji = request.get("emoji", "")

        if not phone_number or not message_id:
            raise HTTPException(status_code=400, detail="phone_number and message_id are required")

        result = await messaging_handler.send_reaction_message(phone_number, message_id, emoji)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending reaction: {e}")
        return {"status": "error", "error": "An error occurred while sending the reaction"}

# ================================
# TEMPLATE ENDPOINTS
# ================================

@app.get("/api/v1/templates/")
async def get_templates():
    """Get all message templates"""
    if not template_handler:
        raise HTTPException(status_code=503, detail="Template handler not available")

    try:
        result = await template_handler.get_message_templates()
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error getting templates: {e}")
        return {"status": "error", "error": "An error occurred while fetching templates"}

@app.post("/api/v1/templates/send")
async def send_template(request: Dict[str, Any]):
    """Send a template message"""
    if not template_handler:
        raise HTTPException(status_code=503, detail="Template handler not available")

    try:
        phone_number = request.get("phone_number")
        template_name = request.get("template_name")
        language_code = request.get("language_code", "en_US")
        body_parameters = request.get("body_parameters", [])
        header_parameters = request.get("header_parameters", [])
        reply_to_message_id = request.get("reply_to_message_id")

        if not phone_number or not template_name:
            raise HTTPException(status_code=400, detail="phone_number and template_name are required")

        # Use the correct method signature
        result = await template_handler.send_template_with_text_parameters(
            phone_number, template_name, language_code, header_parameters, body_parameters, reply_to_message_id
        )
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending template: {e}")
        return {"status": "error", "error": "An error occurred while sending the template"}

# ================================
# BUSINESS ENDPOINTS
# ================================

@app.get("/api/v1/business/profile")
async def get_business_profile():
    """Get business profile"""
    if not business_handler:
        raise HTTPException(status_code=503, detail="Business handler not available")

    try:
        result = await business_handler.get_business_profile()
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error getting business profile: {e}")
        return {"status": "error", "error": "An error occurred while fetching business profile"}

@app.get("/api/v1/business/phone-numbers")
async def get_phone_numbers():
    """Get business phone numbers"""
    if not business_handler:
        raise HTTPException(status_code=503, detail="Business handler not available")

    try:
        result = await business_handler.get_phone_numbers()
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error getting phone numbers: {e}")
        return {"status": "error", "error": "An error occurred while fetching phone numbers"}

# ================================
# FLOWS ENDPOINTS
# ================================

@app.get("/api/v1/flows/")
async def list_flows():
    """List all flows"""
    if not flow_handler:
        raise HTTPException(status_code=503, detail="Flow handler not available")

    try:
        result = await flow_handler.list_flows()
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error listing flows: {e}")
        return {"status": "error", "error": "An error occurred while listing flows"}

@app.post("/api/v1/flows/create")
async def create_flow(request: Dict[str, Any]):
    """Create a new flow"""
    if not flow_handler:
        raise HTTPException(status_code=503, detail="Flow handler not available")

    try:
        name = request.get("name")
        categories = request.get("categories", [])
        clone_flow_id = request.get("clone_flow_id")
        endpoint_uri = request.get("endpoint_uri")

        if not name:
            raise HTTPException(status_code=400, detail="name is required")

        result = await flow_handler.create_flow(name, categories, clone_flow_id, endpoint_uri)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating flow: {e}")
        return {"status": "error", "error": "An error occurred while creating the flow"}

# ================================
# ANALYTICS ENDPOINTS
# ================================

@app.get("/api/v1/analytics/")
async def get_analytics():
    """Get analytics data"""
    if not analytics_handler:
        raise HTTPException(status_code=503, detail="Analytics handler not available")

    try:
        # Get last 7 days analytics
        end_time = int(time.time())
        start_time = end_time - (7 * 24 * 60 * 60)  # 7 days ago

        result = await analytics_handler.get_analytics(start_time, end_time, "DAY")
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error getting analytics: {e}")
        return {"status": "error", "error": "An error occurred while fetching analytics"}

# ================================
# WEBHOOK ENDPOINTS
# ================================

@app.get("/api/v1/webhooks/subscriptions")
async def get_webhook_subscriptions():
    """Get webhook subscriptions"""
    if not webhook_handler:
        raise HTTPException(status_code=503, detail="Webhook handler not available")

    try:
        result = await webhook_handler.get_subscriptions()
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error getting webhook subscriptions: {e}")
        return {"status": "error", "error": "An error occurred while fetching webhook subscriptions"}

# ================================
# BUSINESS ACCOUNT ENDPOINTS
# ================================

@app.get("/api/v1/business-account/wabas")
async def get_waba_accounts():
    """Get WABA accounts"""
    if not business_account_handler:
        raise HTTPException(status_code=503, detail="Business Account handler not available")

    try:
        result = await business_account_handler.get_owned_wabas()
        return {"status": "success", "data": result}
    except Exception as e:
        logger.error(f"Error getting WABA accounts: {e}")
        return {"status": "error", "error": "An error occurred while fetching WABA accounts"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    # Behind a reverse proxy on the same machine, set HOST=127.0.0.1 so the app isn't reachable directly
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=port)
