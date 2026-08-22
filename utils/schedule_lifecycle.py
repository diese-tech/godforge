"""Scheduled-night reminder delivery as a shared lifecycle hook.

Feature module for the Issue #48 refactor. Wraps the periodic reminder-DM loop
that previously lived inline in ``bot.py``'s cleanup task, registering it
through the shared ``FeatureRegistry``. Domain logic (which reminders are due)
stays in ``utils/party_schedule.py``; this module only delivers the DMs.
"""

from __future__ import annotations

import logging

import discord

from utils.lifecycle import LifecycleContext

log = logging.getLogger("godforge.schedule_lifecycle")


class ScheduleLifecycle:
    """Delivers due scheduled-night reminder DMs on the periodic cleanup pass.

    Issue #67: also reconciles every guild's scheduled-night RSVP cards, on
    both startup and every periodic tick — restart recovery and self-healing
    a manually-deleted card are the same "ensure a live, correct card" pass,
    just triggered from two different lifecycle hooks. ``rsvp_service`` is
    optional so callers/tests that only care about reminders can construct
    this with just a repository, unchanged from before Issue #67.
    """

    name = "schedule"

    def __init__(self, schedule_repository, rsvp_service=None):
        self._schedule_repository = schedule_repository
        self._rsvp_service = rsvp_service

    async def on_startup(self, ctx: LifecycleContext) -> None:
        await self._reconcile_rsvp_cards(ctx)

    async def on_cleanup(self, ctx: LifecycleContext) -> None:
        await self._reconcile_rsvp_cards(ctx)
        for event, minutes, occurrence in self._schedule_repository.claim_due_reminders():
            recipients = {event.organizer_id, *(rsvp.user_id for rsvp in event.rsvps)}
            recipients.update(rsvp.user_id for rsvp in event.waitlist)
            for user_id in recipients:
                try:
                    user = ctx.get_user(user_id) or await ctx.fetch_user(user_id)
                    await user.send(
                        f"**{event.title}** starts <t:{int(occurrence.timestamp())}:R> "
                        f"({minutes}-minute reminder)."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.info(
                        "Could not DM scheduled-night reminder to user %s", user_id
                    )

    async def _reconcile_rsvp_cards(self, ctx: LifecycleContext) -> None:
        if self._rsvp_service is None:
            return
        for event in self._schedule_repository.list_all_upcoming():
            await self._rsvp_service.reconcile(event, ctx)
