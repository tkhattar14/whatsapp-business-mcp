"""
Settings for the customer support bot, read from environment variables.
"""

import os
from dataclasses import dataclass
from typing import Optional

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class SupportSettings:
    # Webhook
    verify_token: Optional[str]
    app_secret: Optional[str]

    # Bot behaviour
    db_path: str
    database_url: Optional[str]
    flow_file: str
    agent_idle_hours: float
    greeting_gap_hours: float
    log_timezone: str

    # Relay mode: the human agent replies from their own WhatsApp
    agent_number: Optional[str]
    agent_alert_template: Optional[str]
    agent_alert_language: str

    # SharePoint Excel logging (Microsoft Graph, app-only auth)
    sharepoint_tenant_id: Optional[str]
    sharepoint_client_id: Optional[str]
    sharepoint_client_secret: Optional[str]
    sharepoint_hostname: Optional[str]
    sharepoint_site_path: Optional[str]
    sharepoint_file_path: Optional[str]
    sharepoint_file_url: Optional[str]
    sharepoint_table_name: str
    sharepoint_tickets_table: str
    excel_sync_interval: float

    @property
    def sharepoint_configured(self) -> bool:
        credentials = all([self.sharepoint_tenant_id, self.sharepoint_client_id, self.sharepoint_client_secret])
        by_path = all([self.sharepoint_hostname, self.sharepoint_site_path, self.sharepoint_file_path])
        return credentials and (bool(self.sharepoint_file_url) or by_path)

    @classmethod
    def from_env(cls) -> "SupportSettings":
        return cls(
            verify_token=os.getenv("WEBHOOK_VERIFY_TOKEN"),
            app_secret=os.getenv("META_APP_SECRET"),
            db_path=os.getenv("SUPPORT_DB_PATH", os.path.join("data", "support.db")),
            database_url=os.getenv("DATABASE_URL") or None,
            flow_file=os.getenv("SUPPORT_FLOW_FILE", os.path.join(_PACKAGE_DIR, "flow.json")),
            agent_idle_hours=float(os.getenv("AGENT_IDLE_HOURS", "12")),
            greeting_gap_hours=float(os.getenv("GREETING_GAP_HOURS", "24")),
            log_timezone=os.getenv("LOG_TIMEZONE", "UTC"),
            agent_number="".join(c for c in os.getenv("AGENT_WHATSAPP_NUMBER", "") if c.isdigit()) or None,
            agent_alert_template=os.getenv("AGENT_ALERT_TEMPLATE") or None,
            agent_alert_language=os.getenv("AGENT_ALERT_TEMPLATE_LANGUAGE", "en"),
            sharepoint_tenant_id=os.getenv("SHAREPOINT_TENANT_ID"),
            sharepoint_client_id=os.getenv("SHAREPOINT_CLIENT_ID"),
            sharepoint_client_secret=os.getenv("SHAREPOINT_CLIENT_SECRET"),
            sharepoint_hostname=os.getenv("SHAREPOINT_HOSTNAME"),
            sharepoint_site_path=os.getenv("SHAREPOINT_SITE_PATH"),
            sharepoint_file_path=os.getenv("SHAREPOINT_FILE_PATH"),
            sharepoint_file_url=os.getenv("SHAREPOINT_FILE_URL") or None,
            sharepoint_table_name=os.getenv("SHAREPOINT_TABLE_NAME", "Conversations"),
            sharepoint_tickets_table=os.getenv("SHAREPOINT_TICKETS_TABLE", "Tickets"),
            excel_sync_interval=float(os.getenv("EXCEL_SYNC_INTERVAL_SECONDS", "15")),
        )
