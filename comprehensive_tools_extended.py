"""
Extended Comprehensive WhatsApp Cloud API tools for MCP Server.
This file contains ALL tool definitions including the new features: Flows, Analytics, Webhooks, and Business Account Management.
"""

from typing import List, Dict, Any, Optional
import time

def register_comprehensive_tools_extended(mcp, messaging_handler, template_handler, business_handler, media_handler, flow_handler, analytics_handler, webhook_handler, business_account_handler):
    """Register all comprehensive WhatsApp Cloud API tools with the MCP server"""
    
    # ================================
    # EXISTING MESSAGING TOOLS (from comprehensive_tools.py)
    # ================================

    @mcp.tool()
    async def send_text_message(
        phone_number: str, 
        message: str, 
        preview_url: bool = False,
        reply_to_message_id: Optional[str] = None
    ) -> dict:
        """Send a text message with advanced options."""
        try:
            return await messaging_handler.send_text_message(
                phone_number, message, preview_url, reply_to_message_id
            )
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def send_reaction(phone_number: str, message_id: str, emoji: str) -> dict:
        """React to a message with an emoji."""
        try:
            return await messaging_handler.send_reaction_message(phone_number, message_id, emoji)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # EXISTING TEMPLATE TOOLS
    # ================================

    @mcp.tool()
    async def get_message_templates(
        name: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        language: Optional[str] = None,
        limit: int = 100
    ) -> dict:
        """Get message templates with optional filtering."""
        try:
            return await template_handler.get_message_templates(name, status, category, language, limit)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def send_template_message(
        phone_number: str,
        template_name: str,
        language_code: str = "en_US",
        body_parameters: Optional[List[str]] = None,
        header_parameters: Optional[List[str]] = None,
        reply_to_message_id: Optional[str] = None
    ) -> dict:
        """Send a template message with text parameters."""
        try:
            return await template_handler.send_template_message(
                phone_number, template_name, language_code, body_parameters, header_parameters, reply_to_message_id
            )
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # EXISTING BUSINESS TOOLS
    # ================================

    @mcp.tool()
    async def get_business_profile() -> dict:
        """Get WhatsApp business profile information."""
        try:
            return await business_handler.get_business_profile()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_phone_numbers() -> dict:
        """Get all phone numbers for the WABA."""
        try:
            return await business_handler.get_phone_numbers()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # EXISTING MEDIA TOOLS
    # ================================

    @mcp.tool()
    async def upload_media(file_path: str, media_type: str = "auto") -> dict:
        """Upload media file to WhatsApp."""
        try:
            return await media_handler.upload_media(file_path, media_type)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_media_info(media_id: str) -> dict:
        """Get information about uploaded media."""
        try:
            return await media_handler.get_media_info(media_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # NEW FLOW TOOLS
    # ================================

    @mcp.tool()
    async def create_flow(
        name: str,
        categories: List[str],
        clone_flow_id: Optional[str] = None,
        endpoint_uri: Optional[str] = None
    ) -> dict:
        """
        Create a new WhatsApp Flow.
        
        Args:
            name: Name of the flow
            categories: List of flow categories (SIGN_UP, SIGN_IN, APPOINTMENT_BOOKING, CONTACT_US, OTHER)
            clone_flow_id: Optional ID of existing flow to clone
            endpoint_uri: Optional endpoint URI for the flow
        """
        try:
            return await flow_handler.create_flow(name, categories, clone_flow_id, endpoint_uri)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def list_flows(limit: int = 100) -> dict:
        """List all flows for the WABA."""
        try:
            return await flow_handler.list_flows(limit)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_flow(flow_id: str) -> dict:
        """Get details of a specific flow."""
        try:
            return await flow_handler.get_flow(flow_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def update_flow(
        flow_id: str,
        name: Optional[str] = None,
        categories: Optional[List[str]] = None
    ) -> dict:
        """Update a flow's name or categories."""
        try:
            return await flow_handler.update_flow(flow_id, name, categories)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def publish_flow(flow_id: str) -> dict:
        """Publish a flow (makes it immutable)."""
        try:
            return await flow_handler.publish_flow(flow_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def upload_flow_json(flow_id: str, flow_json_content: str) -> dict:
        """Upload flow JSON content to a flow."""
        try:
            return await flow_handler.upload_flow_json(flow_id, flow_json_content)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_flow_preview(flow_id: str, invalidate: bool = False) -> dict:
        """Get preview URL for a flow."""
        try:
            return await flow_handler.get_flow_preview(flow_id, invalidate)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def migrate_flows(
        source_waba_id: str,
        source_flow_names: Optional[List[str]] = None
    ) -> dict:
        """Migrate flows from source WABA to current WABA."""
        try:
            return await flow_handler.migrate_flows(source_waba_id, source_flow_names)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_flow_metrics(
        flow_id: str,
        metric_name: str = "ENDPOINT_REQUEST_COUNT",
        granularity: str = "DAY",
        since: str = None,
        until: str = None
    ) -> dict:
        """Get metrics for a specific flow."""
        try:
            return await flow_handler.get_flow_metrics(flow_id, metric_name, granularity, since, until)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # NEW ANALYTICS TOOLS
    # ================================

    @mcp.tool()
    async def get_analytics(
        start_time: int,
        end_time: int,
        granularity: str = "DAY",
        phone_numbers: Optional[List[str]] = None,
        country_codes: Optional[List[str]] = None
    ) -> dict:
        """Get WhatsApp analytics data."""
        try:
            return await analytics_handler.get_analytics(start_time, end_time, granularity, phone_numbers, country_codes)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_conversation_analytics(
        start_time: int,
        end_time: int,
        granularity: str = "MONTHLY",
        conversation_directions: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None
    ) -> dict:
        """Get conversation analytics data."""
        try:
            return await analytics_handler.get_conversation_analytics(start_time, end_time, granularity, conversation_directions, dimensions)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_quality_rating(
        phone_number_id: str,
        start_time: int,
        end_time: int
    ) -> dict:
        """Get quality rating for a phone number."""
        try:
            return await analytics_handler.get_quality_rating(phone_number_id, start_time, end_time)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_phone_number_analytics(
        phone_number_id: str,
        start_time: int,
        end_time: int,
        granularity: str = "DAY"
    ) -> dict:
        """Get analytics for a specific phone number."""
        try:
            return await analytics_handler.get_phone_number_analytics(phone_number_id, start_time, end_time, granularity)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_business_analytics(
        start_time: int,
        end_time: int,
        granularity: str = "DAY",
        metrics: Optional[List[str]] = None
    ) -> dict:
        """Get business-level analytics."""
        try:
            return await analytics_handler.get_business_analytics(start_time, end_time, granularity, metrics)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # NEW WEBHOOK TOOLS
    # ================================

    @mcp.tool()
    async def subscribe_to_waba() -> dict:
        """Subscribe your app to a WABA to receive webhook events."""
        try:
            return await webhook_handler.subscribe_to_waba()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_subscriptions() -> dict:
        """Get all subscriptions for a WABA."""
        try:
            return await webhook_handler.get_subscriptions()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def unsubscribe_from_waba() -> dict:
        """Unsubscribe your app from a WABA."""
        try:
            return await webhook_handler.unsubscribe_from_waba()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def override_callback_url(callback_url: str) -> dict:
        """Override the callback URL for webhook events."""
        try:
            return await webhook_handler.override_callback_url(callback_url)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_webhook_verification_token() -> dict:
        """Get webhook verification token for the app."""
        try:
            return await webhook_handler.get_webhook_verification_token()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def set_webhook_verification_token(verification_token: str) -> dict:
        """Set webhook verification token for the app."""
        try:
            return await webhook_handler.set_webhook_verification_token(verification_token)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_webhook_fields() -> dict:
        """Get webhook fields configuration for the app."""
        try:
            return await webhook_handler.get_webhook_fields()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def set_webhook_fields(fields: List[str]) -> dict:
        """Set webhook fields for the app."""
        try:
            return await webhook_handler.set_webhook_fields(fields)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # NEW BUSINESS ACCOUNT TOOLS
    # ================================

    @mcp.tool()
    async def get_owned_wabas(limit: int = 100) -> dict:
        """Get all owned WABAs for the business portfolio."""
        try:
            return await business_account_handler.get_owned_wabas(limit)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_shared_wabas(limit: int = 100) -> dict:
        """Get all shared WABAs for the business portfolio."""
        try:
            return await business_account_handler.get_shared_wabas(limit)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_waba_details(waba_id: str) -> dict:
        """Get detailed information about a specific WABA."""
        try:
            return await business_account_handler.get_waba_details(waba_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def create_waba(
        name: str,
        currency: str,
        timezone_id: str,
        message_template_namespace: Optional[str] = None
    ) -> dict:
        """Create a new WABA."""
        try:
            return await business_account_handler.create_waba(name, currency, timezone_id, message_template_namespace)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def update_waba(
        waba_id: str,
        name: Optional[str] = None,
        currency: Optional[str] = None,
        timezone_id: Optional[str] = None
    ) -> dict:
        """Update a WABA's details."""
        try:
            return await business_account_handler.update_waba(waba_id, name, currency, timezone_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def delete_waba(waba_id: str) -> dict:
        """Delete a WABA."""
        try:
            return await business_account_handler.delete_waba(waba_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def migrate_account(
        source_waba_id: str,
        target_waba_id: str,
        phone_numbers: Optional[List[str]] = None
    ) -> dict:
        """Migrate account data from source WABA to target WABA."""
        try:
            return await business_account_handler.migrate_account(source_waba_id, target_waba_id, phone_numbers)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_waba_insights(
        waba_id: str,
        start_time: int,
        end_time: int,
        granularity: str = "DAY"
    ) -> dict:
        """Get insights for a specific WABA."""
        try:
            return await business_account_handler.get_waba_insights(waba_id, start_time, end_time, granularity)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # NEW ADVANCED PHONE NUMBER TOOLS
    # ================================

    @mcp.tool()
    async def get_phone_number_by_id(phone_number_id: str) -> dict:
        """Get details of a specific phone number."""
        try:
            return await business_handler.get_phone_number_by_id(phone_number_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_display_name_status(phone_number_id: str) -> dict:
        """Get display name status for a phone number (Beta)."""
        try:
            return await business_handler.get_display_name_status(phone_number_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def get_phone_numbers_with_filtering(
        limit: int = 100,
        after: Optional[str] = None,
        before: Optional[str] = None
    ) -> dict:
        """Get phone numbers with filtering (beta)."""
        try:
            return await business_handler.get_phone_numbers_with_filtering(limit, after, before)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def request_verification_code(phone_number_id: str) -> dict:
        """Request verification code for a phone number."""
        try:
            return await business_handler.request_verification_code(phone_number_id=phone_number_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def verify_code(phone_number_id: str, code: str) -> dict:
        """Verify code for a phone number."""
        try:
            return await business_handler.verify_phone_number(code, phone_number_id=phone_number_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def set_two_step_verification_code(phone_number_id: str, code: str) -> dict:
        """Set two-step verification code for a phone number."""
        try:
            return await business_handler.set_two_step_verification(code, phone_number_id=phone_number_id)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ================================
    # UTILITY TOOLS
    # ================================

    @mcp.tool()
    async def get_current_timestamp() -> dict:
        """Get current Unix timestamp for analytics queries."""
        try:
            return {
                "status": "success",
                "timestamp": int(time.time()),
                "message": "Current Unix timestamp"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @mcp.tool()
    async def convert_date_to_timestamp(date_string: str) -> dict:
        """
        Convert date string to Unix timestamp.
        
        Args:
            date_string: Date in format YYYY-MM-DD or YYYY-MM-DD HH:MM:SS
        """
        try:
            import datetime
            if len(date_string) == 10:  # YYYY-MM-DD
                dt = datetime.datetime.strptime(date_string, "%Y-%m-%d")
            else:  # YYYY-MM-DD HH:MM:SS
                dt = datetime.datetime.strptime(date_string, "%Y-%m-%d %H:%M:%S")
            
            timestamp = int(dt.timestamp())
            return {
                "status": "success",
                "timestamp": timestamp,
                "date_string": date_string,
                "message": f"Converted {date_string} to timestamp {timestamp}"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)} 