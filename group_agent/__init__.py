"""
WhatsApp group agent: a dedicated number in residents' groups that reads every
message and, using an OpenAI-compatible LLM, replies when needed.
"""

from .config import GroupAgentSettings
from .routes import router
from .service import GroupAgentService

__all__ = ["GroupAgentSettings", "GroupAgentService", "router"]
