"""
Business handler for WhatsApp Cloud API - handles business profile and phone number operations.
"""

from typing import Dict, Any, List, Optional
from .base_handler import BaseWhatsAppHandler

class BusinessHandler(BaseWhatsAppHandler):
    """Handler for WhatsApp business operations"""
    
    def __init__(self):
        super().__init__()
        # Business operations can work with either META_BUSINESS_ACCOUNT_ID or WABA_ID
        if not (self.business_account_id or self.waba_id):
            raise ValueError("Either META_BUSINESS_ACCOUNT_ID or WABA_ID environment variable is required for business operations")
    
    # ================================
    # WABA (WhatsApp Business Account) OPERATIONS
    # ================================
    
    async def get_business_account(self) -> Dict[str, Any]:
        """Get WhatsApp Business Account information"""
        url = f"{self.base_url}/{self.business_account_id}"
        params = {"fields": "id,name,timezone_offset_minutes,message_template_namespace"}
        return await self._make_request("GET", url, params=params)
    
    async def get_owned_wabas(self) -> Dict[str, Any]:
        """Get all owned WhatsApp Business Accounts"""
        # This requires a Business Portfolio ID instead of WABA ID
        # For now, return the current WABA info
        return await self.get_business_account()
    
    async def subscribe_to_waba(self) -> Dict[str, Any]:
        """Subscribe app to WABA for webhook events"""
        return await self._make_request("POST", self.subscriptions_url)
    
    async def get_waba_subscriptions(self) -> Dict[str, Any]:
        """Get all subscriptions for the WABA"""
        return await self._make_request("GET", self.subscriptions_url)
    
    async def unsubscribe_from_waba(self) -> Dict[str, Any]:
        """Unsubscribe app from WABA"""
        return await self._make_request("DELETE", self.subscriptions_url)
    
    # ================================
    # PHONE NUMBER OPERATIONS
    # ================================
    
    async def get_phone_numbers(self) -> Dict[str, Any]:
        """Get all phone numbers associated with the WABA"""
        if not hasattr(self, 'phone_numbers_url'):
            return {
                "status": "error",
                "message": "Phone numbers endpoint not configured",
                "suggestions": [
                    "Set WABA_ID environment variable",
                    "Or ensure META_BUSINESS_ACCOUNT_ID is set"
                ]
            }
        
        try:
            result = await self._make_request("GET", self.phone_numbers_url)
            return result
        except Exception as e:
            # Provide helpful error information
            waba_used = self.waba_id or self.business_account_id
            return {
                "status": "error",
                "message": f"Unable to access phone numbers using ID: {waba_used}",
                "endpoint_used": self.phone_numbers_url,
                "suggestions": [
                    "Verify your WABA_ID is correct" if self.waba_id else "Try setting WABA_ID environment variable",
                    "Check if your access token has the required permissions",
                    "Ensure the ID has access to phone_numbers field"
                ],
                "ids_available": {
                    "waba_id": self.waba_id,
                    "business_account_id": self.business_account_id
                },
                "error": str(e)
            }
    
    async def get_phone_number_info(self, phone_number_id: Optional[str] = None) -> Dict[str, Any]:
        """Get information about a specific phone number"""
        pid = phone_number_id or self.phone_number_id
        url = f"{self.base_url}/{pid}"
        params = {"fields": "id,verified_name,display_phone_number,quality_rating,platform_type,throughput,webhook_configuration,name_status,new_name_status"}
        return await self._make_request("GET", url, params=params)
    
    async def register_phone_number(self, pin: str) -> Dict[str, Any]:
        """Register a phone number with 2-step verification PIN"""
        url = f"{self.base_url}/{self.phone_number_id}/register"
        payload = {
            "messaging_product": "whatsapp",
            "pin": pin
        }
        return await self._make_request("POST", url, payload)
    
    async def deregister_phone_number(self) -> Dict[str, Any]:
        """Deregister the phone number"""
        url = f"{self.base_url}/{self.phone_number_id}/deregister"
        payload = {"messaging_product": "whatsapp"}
        return await self._make_request("POST", url, payload)
    
    async def request_verification_code(self, code_method: str = "SMS", language: str = "en_US", phone_number_id: Optional[str] = None) -> Dict[str, Any]:
        """Request verification code for phone number"""
        url = f"{self.base_url}/{phone_number_id or self.phone_number_id}/request_code"
        payload = {
            "code_method": code_method,
            "language": language
        }
        return await self._make_request("POST", url, payload)
    
    async def verify_phone_number(self, code: str, phone_number_id: Optional[str] = None) -> Dict[str, Any]:
        """Verify phone number with received code"""
        url = f"{self.base_url}/{phone_number_id or self.phone_number_id}/verify_code"
        payload = {"code": code}
        return await self._make_request("POST", url, payload)
    
    async def set_two_step_verification(self, pin: str, phone_number_id: Optional[str] = None) -> Dict[str, Any]:
        """Set two-step verification PIN"""
        url = f"{self.base_url}/{phone_number_id or self.phone_number_id}"
        payload = {"pin": pin}
        return await self._make_request("POST", url, payload)
    
    async def get_phone_number_by_id(self, phone_number_id: str) -> Dict[str, Any]:
        """Get details of a specific phone number"""
        url = f"{self.base_url}/{phone_number_id}"
        return await self._make_request("GET", url)
    
    async def get_display_name_status(self, phone_number_id: str) -> Dict[str, Any]:
        """Get display name status for a phone number (Beta)"""
        url = f"{self.base_url}/{phone_number_id}"
        params = {"fields": "display_name_status"}
        return await self._make_request("GET", url, params=params)
    
    async def get_phone_numbers_with_filtering(
        self,
        limit: int = 100,
        after: Optional[str] = None,
        before: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get phone numbers with filtering (beta)"""
        params = {"limit": limit}
        
        if after:
            params["after"] = after
        if before:
            params["before"] = before
            
        return await self._make_request("GET", self.phone_numbers_url, params=params)
    
    # ================================
    # BUSINESS PROFILE OPERATIONS
    # ================================
    
    async def get_business_profile(self) -> Dict[str, Any]:
        """Get business profile information"""
        url = f"{self.base_url}/{self.phone_number_id}/whatsapp_business_profile"
        params = {"fields": "about,address,description,email,profile_picture_url,websites,vertical"}
        return await self._make_request("GET", url, params=params)
    
    async def update_business_profile(
        self,
        about: Optional[str] = None,
        address: Optional[str] = None,
        description: Optional[str] = None,
        email: Optional[str] = None,
        profile_picture_url: Optional[str] = None,
        websites: Optional[List[str]] = None,
        vertical: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update business profile"""
        url = f"{self.base_url}/{self.phone_number_id}/whatsapp_business_profile"
        
        payload = {"messaging_product": "whatsapp"}
        
        if about is not None:
            payload["about"] = about
        if address is not None:
            payload["address"] = address
        if description is not None:
            payload["description"] = description
        if email is not None:
            payload["email"] = email
        if profile_picture_url is not None:
            payload["profile_picture_url"] = profile_picture_url
        if websites is not None:
            payload["websites"] = websites
        if vertical is not None:
            payload["vertical"] = vertical
        
        return await self._make_request("POST", url, payload)
    
    # ================================
    # QUALITY RATING OPERATIONS
    # ================================
    
    async def get_phone_number_quality_rating(self) -> Dict[str, Any]:
        """Get quality rating for the phone number"""
        result = await self.get_phone_number_info()
        if result["status"] == "success" and "data" in result:
            quality_rating = result["data"].get("quality_rating", "UNKNOWN")
            return {
                "status": "success",
                "data": {"quality_rating": quality_rating},
                "message": f"Phone number quality rating: {quality_rating}"
            }
        return result
    
    # ================================
    # WEBHOOK CONFIGURATION
    # ================================
    
    async def set_webhook_url(self, callback_url: str) -> Dict[str, Any]:
        """Set webhook callback URL for the phone number"""
        url = f"{self.base_url}/{self.phone_number_id}"
        payload = {
            "messaging_product": "whatsapp",
            "webhook_configuration": {
                "callback_url": callback_url
            }
        }
        return await self._make_request("POST", url, payload)
    
    async def get_webhook_configuration(self) -> Dict[str, Any]:
        """Get current webhook configuration"""
        result = await self.get_phone_number_info()
        if result["status"] == "success" and "data" in result:
            webhook_config = result["data"].get("webhook_configuration", {})
            return {
                "status": "success",
                "data": {"webhook_configuration": webhook_config}
            }
        return result