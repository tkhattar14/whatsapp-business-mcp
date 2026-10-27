"""
Wires the group agent's store, WhatsApp bridge client, LLM, staff list and Excel log
together, and runs the background loop (staff refresh, follow-up timers, Excel sync).
"""

import asyncio
import logging
from typing import Optional

from .agent import BridgeClient, GroupAgent
from .config import MODE_REPLY, GroupAgentSettings
from .staff import SharePointStaffSource, StaffDirectory
from .store import GroupStore

logger = logging.getLogger("group-agent")

LOOP_SECONDS = 30


class GroupAgentService:
    def __init__(self, settings: GroupAgentSettings):
        if not settings.bridge_secret:
            raise ValueError("BRIDGE_SECRET is required for the group agent")
        self.settings = settings
        self.store = GroupStore(settings.database_url or settings.db_path)
        self.bridge = BridgeClient(settings.bridge_url, settings.bridge_secret)
        self.staff = StaffDirectory()
        self.agent = GroupAgent(settings, self.store, self.bridge, staff=self.staff)

        # SharePoint (same connection as the support bot) for the Staff sheet and the Group Issues log
        self.staff_source = None
        self.excel = None
        from support_bot.config import SupportSettings
        from support_bot.sharepoint import SharePointExcelLogger

        sp_settings = SupportSettings.from_env()
        if sp_settings.sharepoint_configured:
            from .excel import GroupIssuesExcel

            connection = SharePointExcelLogger(sp_settings, None, [])
            self.staff_source = SharePointStaffSource(connection, settings.staff_table)
            self.excel = GroupIssuesExcel(connection)
        self.excel_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        s = self.settings
        logger.info("Group agent started - mode: %s, LLM: %s", s.mode, self.agent.llm.name)
        if s.mode == MODE_REPLY:
            logger.info("Sends messages in: %s (observes all other chats)",
                        ", ".join(s.reply_chats) or "nowhere (GROUP_AGENT_REPLY_CHATS is empty)")
        if not self.staff_source:
            logger.warning("SharePoint not configured - no staff list and no Group Issues sheet")
        self._task = asyncio.create_task(self._loop())

    async def tick(self) -> None:
        """One round of background work (also called directly by tests)."""
        if self.staff_source:
            await self.staff_source.refresh(self.staff)
        await self.agent.run_timers()
        if self.excel:
            try:
                written = await self.excel.sync(self.store)
                if written:
                    logger.info("Wrote %d issue(s) to the Group Issues sheet", written)
                self.excel_error = None
            except Exception as e:
                self.excel_error = str(e)
                logger.warning("Group Issues sync failed, will retry: %s", e)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("Group agent background loop failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=LOOP_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self.agent.stop()
        self.store.close()
