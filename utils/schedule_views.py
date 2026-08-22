"""Persistent Discord view for the Issue #67 scheduled-night RSVP card."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord


SCHEDULE_RSVP_CUSTOM_ID_PREFIX = "godforge:schedule:rsvp"

SCHEDULE_RSVP_ACTIONS = (
    ("going", "Going", discord.ButtonStyle.success),
    ("maybe", "Maybe", discord.ButtonStyle.secondary),
    ("cant_make_it", "Can't Make It", discord.ButtonStyle.danger),
)

ScheduleRsvpHandler = Callable[[discord.Interaction, str], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)
_ERROR_MESSAGE = "GodForge could not complete that action. Please try again."


async def _send_safe_error(interaction: discord.Interaction) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(_ERROR_MESSAGE, ephemeral=True)
        else:
            await interaction.followup.send(_ERROR_MESSAGE, ephemeral=True)
    except (discord.HTTPException, AttributeError):
        _LOGGER.exception("Failed to send a GodForge interaction error response")


class _ScheduleRsvpButton(discord.ui.Button):
    def __init__(
        self, action: str, label: str, style: discord.ButtonStyle, handler: ScheduleRsvpHandler
    ) -> None:
        super().__init__(
            label=label, style=style,
            custom_id=f"{SCHEDULE_RSVP_CUSTOM_ID_PREFIX}:{action}:v1",
        )
        self.action = action
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._handler(interaction, self.action)
        except Exception:
            _LOGGER.exception("Schedule RSVP card action failed")
            await _send_safe_error(interaction)


class ScheduleRsvpView(discord.ui.View):
    """The public scheduled-night RSVP card's persistent Going/Maybe/Can't Make It row."""

    def __init__(self, handler: ScheduleRsvpHandler) -> None:
        super().__init__(timeout=None)
        for action, label, style in SCHEDULE_RSVP_ACTIONS:
            self.add_item(_ScheduleRsvpButton(action, label, style, handler))
