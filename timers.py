"""
Per-draft turn timers.

Every active draft gets at most one asyncio task, which sleeps until that
draft's turn deadline and then calls back into the bot to skip the player.
Because the deadline is also written to SQLite, timers can be rebuilt after
a restart without losing anybody's place.
"""

import asyncio
import logging
import time

log = logging.getLogger("draftbot.timers")


class TurnTimers:
    """Keeps one countdown per draft id."""

    def __init__(self, on_expire):
        # on_expire is an async function called as on_expire(draft_id, token).
        self.on_expire = on_expire
        self.tasks = {}

    def schedule(self, draft_id, seconds, token):
        """Start (or restart) the countdown for a draft. Returns the deadline.

        `token` identifies the exact turn, normally (round, turn_index). It is
        handed back when the timer fires so the bot can ignore a timer that
        belongs to a turn which has already finished.
        """
        self.cancel(draft_id)
        deadline = time.time() + seconds
        self.tasks[draft_id] = asyncio.create_task(
            self._countdown(draft_id, seconds, token)
        )
        return deadline

    def cancel(self, draft_id):
        """Stop the countdown for a draft, if one is running."""
        task = self.tasks.pop(draft_id, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_all(self):
        for draft_id in list(self.tasks):
            self.cancel(draft_id)

    async def _countdown(self, draft_id, seconds, token):
        try:
            await asyncio.sleep(max(0, seconds))
        except asyncio.CancelledError:
            return

        self.tasks.pop(draft_id, None)
        try:
            await self.on_expire(draft_id, token)
        except Exception:
            # A crash here would silently freeze the draft, so it is logged
            # loudly rather than swallowed.
            log.exception("timer callback failed for draft %s", draft_id)
