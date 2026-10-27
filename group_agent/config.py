"""
Settings for the WhatsApp group agent, read from environment variables.
"""

import os
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

MODE_LISTEN = "listen"    # read and store everything; no LLM, never sends
MODE_OBSERVE = "observe"  # LLM logs and tracks issues, but never sends anything
MODE_REPLY = "reply"      # full agent; sends only in GROUP_AGENT_REPLY_CHATS (observes elsewhere)
MODES = (MODE_LISTEN, MODE_OBSERVE, MODE_REPLY)

# Gemini's OpenAI-compatible API; "-latest" follows Google's current (free tier) Flash model
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_MODEL = "gemini-flash-latest"


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _api_base(url: str) -> Optional[str]:
    """'http://host:8000' -> 'http://host:8000/v1' (vLLM and most servers serve the API under /v1)."""
    url = url.strip().rstrip("/")
    if not url:
        return None
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if urlparse(url).path in ("", "/"):
        url += "/v1"
    return url


@dataclass(frozen=True)
class GroupAgentSettings:
    enabled: bool
    mode: str
    reply_chats: Tuple[str, ...]  # group ids or names; "*" = every chat
    assistant_name: str
    prompt_file: str

    # WhatsApp bridge (wa_bridge/)
    bridge_url: str
    bridge_secret: Optional[str]

    # Any OpenAI-compatible chat completions API; empty = built-in test mode
    llm_base_url: Optional[str]
    llm_api_key: Optional[str]
    llm_model: Optional[str]
    llm_temperature: float
    llm_timeout: float
    llm_tool_mode: str  # auto | tools | json
    # Second model used whenever the main one fails (Gemini by default); off without an API key
    llm_fallback_base_url: Optional[str]
    llm_fallback_api_key: Optional[str]
    llm_fallback_model: Optional[str]

    # Issue follow-up timers
    reminder_minutes: float   # no staff response -> @mention first-contact staff in the group
    escalate_minutes: float   # still none -> private message to "Escalate to" staff
    feedback_hours: float     # no rating after staff said done -> close without a score
    tag_wait_minutes: float   # resident tagged a staff member -> give them this long before acknowledging
    followup_hours: float     # in progress this long without being done -> private follow-up to that staff member
    followup_repeat_hours: float
    followup_max: int
    staff_table: str

    # Pacing
    history_size: int
    debounce_seconds: float
    min_reply_gap_seconds: float

    # Storage (shared with the support bot)
    database_url: Optional[str]
    db_path: str

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def llm_fallback_configured(self) -> bool:
        return bool(self.llm_configured and self.llm_fallback_api_key and self.llm_fallback_model)

    @classmethod
    def from_env(cls) -> "GroupAgentSettings":
        mode = os.getenv("GROUP_AGENT_MODE", MODE_LISTEN).strip().lower()
        if mode not in MODES:
            raise ValueError(f"GROUP_AGENT_MODE must be one of {', '.join(MODES)}, not '{mode}'")
        chats = tuple(c.strip() for c in os.getenv("GROUP_AGENT_REPLY_CHATS", "").split(",") if c.strip())
        return cls(
            enabled=_flag("GROUP_AGENT_ENABLED"),
            mode=mode,
            reply_chats=chats,
            assistant_name=os.getenv("ASSISTANT_NAME", "Max Migold Assistant"),
            prompt_file=os.getenv("GROUP_AGENT_PROMPT_FILE", os.path.join(_PACKAGE_DIR, "prompt.md")),
            bridge_url=os.getenv("BRIDGE_URL", "http://127.0.0.1:3100").rstrip("/"),
            bridge_secret=os.getenv("BRIDGE_SECRET") or None,
            llm_base_url=_api_base(os.getenv("LLM_BASE_URL") or ""),
            llm_api_key=os.getenv("LLM_API_KEY") or None,
            llm_model=os.getenv("LLM_MODEL") or None,
            llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
            llm_timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            llm_tool_mode=os.getenv("LLM_TOOL_MODE", "auto").strip().lower(),
            llm_fallback_base_url=_api_base(os.getenv("LLM_FALLBACK_BASE_URL") or GEMINI_BASE_URL),
            llm_fallback_api_key=os.getenv("LLM_FALLBACK_API_KEY") or None,
            llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL") or GEMINI_MODEL,
            reminder_minutes=float(os.getenv("GROUP_REMINDER_MINUTES", "30")),
            escalate_minutes=float(os.getenv("GROUP_ESCALATE_MINUTES", "60")),
            feedback_hours=float(os.getenv("GROUP_FEEDBACK_HOURS", "24")),
            tag_wait_minutes=float(os.getenv("GROUP_TAG_WAIT_MINUTES", "10")),
            followup_hours=float(os.getenv("GROUP_FOLLOWUP_HOURS", "48")),
            followup_repeat_hours=float(os.getenv("GROUP_FOLLOWUP_REPEAT_HOURS", "24")),
            followup_max=int(os.getenv("GROUP_FOLLOWUP_MAX", "3")),
            staff_table=os.getenv("GROUP_STAFF_TABLE", "Staff"),
            history_size=int(os.getenv("GROUP_AGENT_HISTORY", "30")),
            debounce_seconds=float(os.getenv("GROUP_AGENT_DEBOUNCE_SECONDS", "6")),
            min_reply_gap_seconds=float(os.getenv("GROUP_AGENT_MIN_REPLY_GAP_SECONDS", "60")),
            database_url=os.getenv("DATABASE_URL") or None,
            db_path=os.getenv("SUPPORT_DB_PATH", os.path.join("data", "support.db")),
        )
