"""
Customer support bot: WhatsApp menu bot with human handoff (Coexistence)
and conversation logging to a SharePoint Excel table.
"""

from .config import SupportSettings
from .routes import router
from .service import SupportService

__all__ = ["SupportSettings", "SupportService", "router"]
