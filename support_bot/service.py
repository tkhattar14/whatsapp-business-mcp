"""
Wires the store, bot and SharePoint logger together and manages the sync task.
"""

import asyncio
import logging
from typing import Optional

from mcp_server import MessagingHandler

from .bot import SupportBot
from .config import SupportSettings
from .sharepoint import SharePointExcelLogger, ticket_columns
from .store import SupportStore

logger = logging.getLogger("whatsapp-support")


class SupportService:
    def __init__(self, settings: SupportSettings, messaging: MessagingHandler):
        self.settings = settings
        # PostgreSQL in production (DATABASE_URL), SQLite file otherwise
        self.store = SupportStore(settings.database_url or settings.db_path)
        self.bot = SupportBot(settings, self.store, messaging)
        self.excel = (
            SharePointExcelLogger(settings, self.store, self.bot.fields) if settings.sharepoint_configured else None
        )
        if self.excel:
            self.bot.status_reader = self.excel.get_ticket_status
        self._stop = asyncio.Event()
        self._sync_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.store.backend == "postgres":
            logger.info("Storing conversations and tickets in PostgreSQL (DATABASE_URL)")
        else:
            logger.info("Storing conversations and tickets in SQLite file %s", self.settings.db_path)
        if not self.settings.verify_token:
            logger.warning("WEBHOOK_VERIFY_TOKEN not set - Meta cannot verify the /webhook endpoint")
        if not self.settings.app_secret:
            logger.warning("META_APP_SECRET not set - incoming webhooks will be rejected")
        if self.excel:
            self._sync_task = asyncio.create_task(self.excel.run(self._stop))
            logger.info(
                "SharePoint Excel logging enabled - tables '%s' and '%s' (Tickets columns: %s)",
                self.settings.sharepoint_table_name, self.settings.sharepoint_tickets_table,
                " | ".join(ticket_columns(self.bot.fields)),
            )
        else:
            logger.warning("SharePoint not configured - messages are only kept in the database")

    async def stop(self) -> None:
        self._stop.set()
        if self._sync_task:
            try:
                await asyncio.wait_for(self._sync_task, timeout=35)
            except asyncio.TimeoutError:
                self._sync_task.cancel()
        self.store.close()
