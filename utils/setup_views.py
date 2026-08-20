"""Persistent Discord views used by GodForge's zero-config guild setup.

The views contain no setup or role-management policy.  Callers inject async
handlers so the same persistent components can be registered after a restart
without coupling Discord UI state to storage implementations.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord


PLAY_CUSTOM_ID_PREFIX = "godforge:party:panel"
ROLE_CUSTOM_ID_PREFIX = "godforge:roles:preference"

PLAY_ACTIONS = (
    ("create", "Start Queue", discord.ButtonStyle.success),
    ("browse", "Browse Queues", discord.ButtonStyle.primary),
    # Issue #63: this used to silently join the first open lobby with space.
    # It's kept as action key "queue" so already-posted Play panels keep
    # routing correctly, but it now always makes the player choose an exact
    # queue (or one-click-shortcuts only when exactly one is open).
    ("queue", "Find a Queue", discord.ButtonStyle.primary),
    ("preferences", "My Preferences", discord.ButtonStyle.secondary),
)

ROLE_PREFERENCES = (
    ("solo", "Solo", discord.ButtonStyle.secondary),
    ("jungle", "Jungle", discord.ButtonStyle.secondary),
    ("mid", "Mid", discord.ButtonStyle.secondary),
    ("support", "Support", discord.ButtonStyle.secondary),
    ("adc", "ADC", discord.ButtonStyle.secondary),
    ("captain", "Captain", discord.ButtonStyle.secondary),
    ("substitute", "Substitute", discord.ButtonStyle.secondary),
    ("region", "Region", discord.ButtonStyle.secondary),
    ("lfg", "LFG", discord.ButtonStyle.secondary),
)

InteractionHandler = Callable[[discord.Interaction], Awaitable[None]]
PlayInteractionHandler = Callable[[discord.Interaction, str], Awaitable[None]]
RoleInteractionHandler = Callable[[discord.Interaction, str], Awaitable[None]]

_LOGGER = logging.getLogger(__name__)
_ERROR_MESSAGE = "GodForge could not complete that action. Please try again."


async def _send_ephemeral_error(interaction: discord.Interaction) -> None:
    """Report callback failures without leaking exception details to Discord."""

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(_ERROR_MESSAGE, ephemeral=True)
        else:
            await interaction.followup.send(_ERROR_MESSAGE, ephemeral=True)
    except (discord.HTTPException, AttributeError):
        _LOGGER.exception("Failed to send a GodForge interaction error response")


class _DelegatingButton(discord.ui.Button):
    def __init__(
        self,
        *,
        label: str,
        custom_id: str,
        style: discord.ButtonStyle,
        handler: InteractionHandler,
        emoji: str | None = None,
    ) -> None:
        super().__init__(label=label, custom_id=custom_id, style=style, emoji=emoji)
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await self._handler(interaction)
        except Exception:
            _LOGGER.exception("GodForge persistent button handler failed")
            await _send_ephemeral_error(interaction)


class PlayPanelView(discord.ui.View):
    """Persistent entry point for joining or opening a GodForge party lobby."""

    def __init__(self, handler: PlayInteractionHandler) -> None:
        super().__init__(timeout=None)
        for action_key, label, style in PLAY_ACTIONS:

            async def delegate(
                interaction: discord.Interaction,
                selected_action: str = action_key,
            ) -> None:
                await handler(interaction, selected_action)

            self.add_item(
                _DelegatingButton(
                    label=label,
                    custom_id=f"{PLAY_CUSTOM_ID_PREFIX}:{action_key}:v1",
                    style=style,
                    handler=delegate,
                )
            )


class RolePreferencesView(discord.ui.View):
    """Persistent self-service buttons for managed cosmetic role preferences."""

    def __init__(self, handler: RoleInteractionHandler) -> None:
        super().__init__(timeout=None)
        for role_key, label, style in ROLE_PREFERENCES:

            async def delegate(
                interaction: discord.Interaction,
                selected_role: str = role_key,
            ) -> None:
                await handler(interaction, selected_role)

            self.add_item(
                _DelegatingButton(
                    label=label,
                    custom_id=f"{ROLE_CUSTOM_ID_PREFIX}:{role_key}:v1",
                    style=style,
                    handler=delegate,
                )
            )
